"""Telegram identity for the closed editorial desk (docs/REQUIREMENTS.md section 11).

Two officially supported flows are implemented, both verified **server-side**:

* **OIDC** (``TelegramOidc``) — authorization code flow with PKCE, ``state`` and
  ``nonce``, a redirect-URI allowlist, HTTPS-only endpoints, and an ``id_token``
  verified against the provider JWKS with issuer, audience, expiry, not-before
  and nonce checks. Identity is the stable Telegram user id (the ``sub`` claim);
  a username is only ever a display hint.
* **Login widget** (``verify_login_widget``) — the classic widget payload, whose
  signature is HMAC-SHA256 over the sorted fields with ``SHA256(bot_token)`` as
  key, plus a freshness window on ``auth_date``.

Nothing here grants rights: a verified identity is only a claim about *who*
logged in, and the allowlist (``randreport.admin.members``) decides *what* that
identity may do. No token, secret or raw payload is ever logged or returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode

from .jose import JoseError, PublicKey, keys_from_jwks, verify_jwt

PENDING_COOKIE = "rd_admin_login"
DEFAULT_PENDING_TTL = timedelta(minutes=15)
WIDGET_MAX_AGE = timedelta(minutes=5)
MAX_RESPONSE_BYTES = 256 * 1024


class AuthError(RuntimeError):
    """A login step that must not succeed. The message is safe to show."""


class InsecureUrl(AuthError):
    """An endpoint that is not HTTPS (outside the loopback staging exception)."""


@dataclass(frozen=True)
class Identity:
    """A verified Telegram identity. ``user_id`` is the stable Telegram user id."""

    user_id: str
    display_name: str
    username: str | None = None
    email: str | None = None
    method: str = "oidc"

    def __post_init__(self) -> None:
        if not str(self.user_id).strip().isdigit():
            raise AuthError("a Telegram identity must carry a numeric user id")


@dataclass(frozen=True)
class PendingAuth:
    """A half-finished login, stored in a signed cookie — never in the URL."""

    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    created_at: str
    expires_at: str
    next_path: str = "/admin/"

    def expired(self, *, now: datetime) -> bool:
        return datetime.fromisoformat(self.expires_at) <= now


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def transport_http(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    """The only real network call in this module: HTTPS, no redirects, size-capped."""

    import urllib.error
    import urllib.request

    if not url.startswith("https://"):
        raise InsecureUrl("the provider endpoint must be HTTPS")
    request = urllib.request.Request(url, data=data, method=method.upper())
    for name, value in (headers or {}).items():
        request.add_header(name, value)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - policy
            raise AuthError("the provider endpoint redirected; the request is refused")

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - scheme checked above
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise AuthError("the provider response is too large")
            return int(response.status), body
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        return int(exc.code), b""
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise AuthError("the identity provider is unreachable") from exc


class TelegramOidc:
    """Authorization-code + PKCE client for the Telegram OIDC provider."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        redirect_uri: str,
        secret: str | bytes,
        transport: Callable[..., tuple[int, bytes]] = transport_http,
        now: Callable[[], datetime] = _utcnow,
        allowed_redirect_uris: Sequence[str] | None = None,
        pending_ttl: timedelta = DEFAULT_PENDING_TTL,
        scopes: Sequence[str] = ("openid",),
        allow_insecure_loopback: bool = False,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self.transport = transport
        self.now = now
        self.allowed_redirect_uris = tuple(allowed_redirect_uris or (redirect_uri,))
        self.pending_ttl = pending_ttl
        self.scopes = tuple(scopes)
        self.allow_insecure_loopback = allow_insecure_loopback
        self._metadata: Mapping[str, Any] | None = None
        self._jwks: dict[str, PublicKey] = {}
        self._check_urls()

    # --- configuration ----------------------------------------------------- #

    @staticmethod
    def _loopback(url: str) -> bool:
        return url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))

    def _check_urls(self) -> None:
        for name, value in (("issuer", self.issuer), ("redirect_uri", self.redirect_uri)):
            if not value.startswith("https://"):
                if not (self.allow_insecure_loopback and self._loopback(value)):
                    raise InsecureUrl(f"{name} must be HTTPS")
        if self.redirect_uri not in self.allowed_redirect_uris:
            raise AuthError("the redirect URI is not in the allowlist")

    # --- discovery and JWKS ------------------------------------------------ #

    def _get_json(self, url: str) -> Mapping[str, Any]:
        if not url.startswith("https://") and not (self.allow_insecure_loopback and self._loopback(url)):
            raise InsecureUrl("the provider endpoint must be HTTPS")
        status, body = self.transport("GET", url, data=None, headers={"Accept": "application/json"})
        if status != 200:
            raise AuthError("the identity provider answered with an error")
        try:
            document = json.loads(body)
        except ValueError as exc:
            raise AuthError("the identity provider answered with invalid JSON") from exc
        if not isinstance(document, Mapping):
            raise AuthError("the identity provider answer is not a JSON object")
        return document

    def metadata(self) -> Mapping[str, Any]:
        if self._metadata is None:
            document = self._get_json(f"{self.issuer}/.well-known/openid-configuration")
            if document.get("issuer", self.issuer).rstrip("/") != self.issuer:
                raise AuthError("the discovery document names a different issuer")
            for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                value = document.get(field)
                if not isinstance(value, str) or not value:
                    raise AuthError(f"the discovery document has no {field}")
            self._metadata = document
        return self._metadata

    def jwks(self, *, refresh: bool = False) -> dict[str, PublicKey]:
        if refresh or not self._jwks:
            document = self._get_json(str(self.metadata()["jwks_uri"]))
            self._jwks = keys_from_jwks(document)
        return self._jwks

    # --- login ------------------------------------------------------------- #

    @staticmethod
    def code_challenge(verifier: str) -> str:
        from .jose import b64url_encode

        return b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())

    def begin(self, *, next_path: str = "/admin/") -> tuple[str, PendingAuth]:
        """Return the authorization URL and the pending-login record for the cookie."""

        verifier = secrets.token_urlsafe(64)
        moment = self.now()
        pending = PendingAuth(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=verifier,
            redirect_uri=self.redirect_uri,
            created_at=moment.isoformat(),
            expires_at=(moment + self.pending_ttl).isoformat(),
            next_path=next_path if next_path.startswith("/admin") else "/admin/",
        )
        metadata = self.metadata()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.scopes),
                "state": pending.state,
                "nonce": pending.nonce,
                "code_challenge": self.code_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}", pending

    def sign_pending(self, pending: PendingAuth) -> str:
        body = json.dumps(vars(pending), sort_keys=True, separators=(",", ":")).encode("utf-8")
        from .jose import b64url_encode

        payload = b64url_encode(body)
        mac = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{payload}.{mac}"

    def read_pending(self, value: str | None) -> PendingAuth:
        from .jose import JoseError as _JoseError
        from .jose import b64url_decode

        if not value or value.count(".") != 1:
            raise AuthError("the login session cookie is missing")
        payload, mac = value.split(".", 1)
        expected = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise AuthError("the login session cookie is not authentic")
        try:
            data = json.loads(b64url_decode(payload))
        except (_JoseError, ValueError) as exc:
            raise AuthError("the login session cookie is malformed") from exc
        if not isinstance(data, Mapping):
            raise AuthError("the login session cookie is malformed")
        pending = PendingAuth(**{key: data[key] for key in PendingAuth.__dataclass_fields__ if key in data})
        if pending.expired(now=self.now()):
            raise AuthError("the login session has expired; start again")
        return pending

    def complete(self, *, code: str, state: str | None, pending: PendingAuth) -> Identity:
        """Exchange the authorization code and verify the resulting ``id_token``."""

        if not code:
            raise AuthError("the provider returned no authorization code")
        if state is None or not hmac.compare_digest(state, pending.state):
            raise AuthError("the state does not match the login session")
        metadata = self.metadata()
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": pending.code_verifier,
            }
        ).encode("ascii")
        status, raw = self.transport(
            "POST",
            str(metadata["token_endpoint"]),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if status != 200:
            raise AuthError("the token endpoint refused the exchange")
        try:
            tokens = json.loads(raw)
        except ValueError as exc:
            raise AuthError("the token endpoint answered with invalid JSON") from exc
        id_token = tokens.get("id_token") if isinstance(tokens, Mapping) else None
        if not isinstance(id_token, str):
            raise AuthError("the token response carries no id_token")
        return self.identity_from_id_token(id_token, nonce=pending.nonce)

    def identity_from_id_token(self, id_token: str, *, nonce: str | None) -> Identity:
        claims = self._verify(id_token, nonce=nonce)
        return Identity(
            user_id=str(claims["sub"]),
            display_name=str(claims.get("name") or claims.get("preferred_username") or ""),
            username=(
                str(claims["preferred_username"])
                if isinstance(claims.get("preferred_username"), str)
                else None
            ),
            email=claims.get("email") if isinstance(claims.get("email"), str) else None,
            method="oidc",
        )

    def _verify(self, id_token: str, *, nonce: str | None) -> Mapping[str, Any]:
        # Telegram migrated its OIDC signing key algorithm over time; accept both
        # documented asymmetric algorithms and refresh the JWKS once on an unknown kid.
        try:
            verified = verify_jwt(
                id_token,
                keys=self.jwks(),
                issuer=self.issuer,
                audience=self.client_id,
                nonce=nonce,
                now=self.now(),
            )
        except JoseError as exc:
            if "key id" not in str(exc):
                raise AuthError(f"the id_token was refused: {exc}") from exc
            try:
                verified = verify_jwt(
                    id_token,
                    keys=self.jwks(refresh=True),
                    issuer=self.issuer,
                    audience=self.client_id,
                    nonce=nonce,
                    now=self.now(),
                )
            except JoseError as second:
                raise AuthError(f"the id_token was refused: {second}") from second
        return verified.claims


def widget_data_check_string(data: Mapping[str, Any]) -> str:
    """The exact string Telegram signs: sorted ``key=value`` lines, ``hash`` excluded."""

    return "\n".join(f"{key}={data[key]}" for key in sorted(data) if key != "hash")


def widget_signature(data: Mapping[str, Any], bot_token: str) -> str:
    secret = hashlib.sha256(bot_token.encode("utf-8")).digest()
    return hmac.new(secret, widget_data_check_string(data).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_login_widget(
    data: Mapping[str, Any],
    *,
    bot_token: str,
    now: Callable[[], datetime] = _utcnow,
    max_age: timedelta = WIDGET_MAX_AGE,
) -> Identity:
    """Verify a classic Telegram Login widget payload and return its identity."""

    if not bot_token:
        raise AuthError("no bot token is configured for widget logins")
    given = data.get("hash")
    if not isinstance(given, str) or not given:
        raise AuthError("the widget payload has no signature")
    expected = widget_signature(data, bot_token)
    if not hmac.compare_digest(given.lower(), expected):
        raise AuthError("the widget signature does not verify")
    try:
        auth_date = datetime.fromtimestamp(int(str(data.get("auth_date"))), tz=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise AuthError("the widget payload has no usable auth_date") from exc
    moment = now()
    if auth_date > moment + timedelta(minutes=2):
        raise AuthError("the widget payload is dated in the future")
    if moment - auth_date > max_age:
        raise AuthError("the widget payload is too old")
    raw_id = data.get("id")
    if not str(raw_id).strip().isdigit():
        raise AuthError("the widget payload has no Telegram user id")
    name = " ".join(str(part) for part in (data.get("first_name"), data.get("last_name")) if part)
    username = data.get("username")
    return Identity(
        user_id=str(raw_id),
        display_name=name or (str(username) if username else ""),
        username=str(username) if username else None,
        method="widget",
    )


def widget_material(user_id: str, *, bot_token: str, auth_date: str, username: str | None = None, first_name: str = "Коммунар") -> dict[str, Any]:
    """Build a *synthetic* widget payload for tests (never used in production paths)."""

    data: dict[str, Any] = {"id": user_id, "first_name": first_name, "auth_date": auth_date}
    if username:
        data["username"] = username
    data["hash"] = widget_signature(data, bot_token)
    return data


def synthetic_login(identity: Identity) -> Identity:
    """A copy of ``identity`` with an unrelated field — helper for tests."""

    return replace(identity)
