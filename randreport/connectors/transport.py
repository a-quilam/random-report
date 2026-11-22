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

        return {"method": self.method, "url": self.url, "params": dict(self.params), "json_body": self.json_body}


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
    """Real HTTP transport. Requires real credentials; never used in unit tests."""

    def __init__(self, *, timeout_s: float = 20.0) -> None:
        self.timeout_s = timeout_s

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
            with urllib.request.urlopen(http_request, timeout=request.timeout_s or self.timeout_s) as response:
                return Response(status=response.status, body=response.read(), headers=dict(response.headers))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raise HttpStatusError(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise TransportError(str(exc.reason)) from exc
