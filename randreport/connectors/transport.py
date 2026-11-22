"""HTTP transport abstraction.

Tests use :class:`FakeTransport` with synthetic payloads only; no test ever
performs a network call. :class:`UrllibTransport` is the real adapter used by the
integration process once real credentials are provided — it is intentionally not
exercised by the unit suite (docs/REQUIREMENTS.md sections 5, 13).
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from .. import redaction

#: Headers that carry a credential. They are never forwarded across a redirect.
SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "x-api-key", "x-auth-token", "x-api-token"}
)


def _origin(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) with scheme-appropriate default ports filled in."""

    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (parts.scheme.lower(), host, port)


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    params: Mapping[str, str] = field(default_factory=dict)
    json_body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 20.0

    def full_url(self) -> str:
        if not self.params:
            return self.url
        return f"{self.url}?{urllib.parse.urlencode(self.params)}"

    def as_record(self) -> dict[str, Any]:
        """A redacted request record for tests and logs (no headers, no token)."""

        return redaction.redact_mapping(
            {"method": self.method, "url": self.url, "params": dict(self.params), "json_body": self.json_body}
        )


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.body.decode("utf-8"))


class TransportError(RuntimeError):
    """A network-level failure (timeout, connection reset)."""


class HttpStatusError(TransportError):
    """A non-2xx response."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class Transport(Protocol):
    def send(self, request: Request) -> Response:  # pragma: no cover - protocol
        ...


def json_response(payload: Any, *, status: int = 200, headers: Mapping[str, str] | None = None) -> Response:
    return Response(status=status, body=_json.dumps(payload).encode("utf-8"), headers=dict(headers or {}))


Handler = Callable[[Request], Response]


class FakeTransport:
    """Deterministic, offline transport for the test suite."""

    def __init__(self, handler: Handler | Sequence[Response] | None = None) -> None:
        self._handler = handler
        self.requests: list[Request] = []

    def send(self, request: Request) -> Response:
        self.requests.append(request)
        handler = self._handler
        if handler is None:
            raise TransportError("no handler configured")
        if callable(handler):
            return handler(request)
        sequence = list(handler)
        if not sequence:
            raise TransportError("response queue is empty")
        return sequence.pop(0)

    # convenience helpers used by the tests
    def last(self) -> Request:
        return self.requests[-1]


class UrllibTransport:
    """Real HTTP transport. Requires real credentials; never used in unit tests.

    Redirects are handled by :class:`_CredentialSafeRedirect`, so a credential is
    never forwarded to another origin, and an ``HTTPError`` body is read and then
    explicitly closed so the response does not leak a socket handle.
    """

    def __init__(self, *, timeout_s: float = 20.0) -> None:
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(_CredentialSafeRedirect())

    def send(self, request: Request) -> Response:
        data = None
        headers = dict(request.headers)
        if request.json_body is not None:
            data = _json.dumps(request.json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        http_request = urllib.request.Request(
            request.full_url(), data=data, headers=headers, method=request.method
        )
        try:
            with self._opener.open(http_request, timeout=request.timeout_s or self.timeout_s) as response:
                return Response(status=response.status, body=response.read(), headers=dict(response.headers))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            # read the body, then close the error response: an HTTPError holds an
            # open file object and leaving it unclosed leaks the connection.
            try:
                body = exc.read().decode("utf-8", errors="replace")
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001 - a body-less error has nothing to close
                    pass
            raise HttpStatusError(exc.code, redaction.redact(body)) from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise TransportError(redaction.redact(str(exc.reason))) from exc


class _CredentialSafeRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that never lets a credential cross to another origin.

    ``urllib`` re-sends the caller's headers -- including ``Authorization`` -- on
    a 3xx by default. Here every sensitive header is removed from the follow-up
    request, and a redirect that leaves the original origin while the request
    carried a credential is refused outright: a service that answers with
    ``302 https://evil.example`` must not be able to make the nightly cycle hand
    over the Zenmoney token (docs/REQUIREMENTS.md section 9).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_origin = _origin(req.full_url)
        new_origin = _origin(newurl)
        carried = [name for name in req.headers if name.lower() in SENSITIVE_HEADERS]
        carried += [name for name in req.unredirected_hdrs if name.lower() in SENSITIVE_HEADERS]

        if old_origin[0] == "https" and new_origin[0] == "http":
            raise TransportError("refusing an https->http redirect (credential downgrade)")

        if carried and new_origin != old_origin:
            raise TransportError(
                "refusing to forward credentials to another origin across a redirect"
            )

        for name in carried:
            # defensive: strip them even on a same-origin redirect, so a
            # credential is never replayed by the transport without a reason.
            for holder in (new.headers, new.unredirected_hdrs):
                holder.pop(name, None)
                holder.pop(name.title(), None)
                holder.pop(name.capitalize(), None)
        return new

