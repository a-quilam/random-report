"""Synthetic fixtures for the editorial-desk tests (A01, A02, R01, H01, E2E).

Everything here is invented: two throwaway key pairs, a fake OIDC provider that
answers over an in-process transport, Telegram ids of no real person, and a
database built by the real pipeline from the offline ingestion fixtures. No test
touches the network, ChatGPT or a production service.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.admin import oidc  # noqa: E402
from randreport.admin import jose  # noqa: E402
from randreport.store import apply_migrations, connect  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as fi  # noqa: E402

# --- synthetic key material (never used anywhere real) --------------------- #

ES_SECRET = 0x5AC9B1D6A2C4959C9E7B1F5D5C0A6E1F3A4C0D47A1E6BF2F9C7A55D3B0C21A4D
ES_KID = "synthetic-es256-1"

_RSA_P = 2**1279 - 1
_RSA_Q = 2**2203 - 1
RSA_N = _RSA_P * _RSA_Q
RSA_E = 65537
RSA_D = pow(RSA_E, -1, (_RSA_P - 1) * (_RSA_Q - 1))
RSA_KID = "synthetic-rs256-1"

ADMIN_ID = "111222333"          # the first commune member (bootstrap)
SECOND_ADMIN_ID = "444555666"
OUTSIDER_ID = "999888777"       # a public-channel subscriber, not a member

BOT_TOKEN = "123456:synthetic-bot-token"
SERVER_SECRET = "synthetic-server-secret-for-tests"
ISSUER = "https://oauth.telegram.test"
CLIENT_ID = "synthetic-client-id"
REDIRECT_URI = "https://desk.example/admin/callback"

ICAO = "https://desk.example"
ORIGIN = "https://desk.example"


def es_jwk() -> dict[str, Any]:
    x, y = jose.public_point(ES_SECRET)
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": ES_KID,
        "alg": "ES256",
        "use": "sig",
        "x": jose.b64url_encode(x.to_bytes(32, "big")),
        "y": jose.b64url_encode(y.to_bytes(32, "big")),
    }


def rs_jwk() -> dict[str, Any]:
    size = (RSA_N.bit_length() + 7) // 8
    return {
        "kty": "RSA",
        "kid": RSA_KID,
        "alg": "RS256",
        "use": "sig",
        "n": jose.b64url_encode(RSA_N.to_bytes(size, "big")),
        "e": jose.b64url_encode(RSA_E.to_bytes(3, "big")),
    }


def sign_jwt(claims: Mapping[str, Any], *, alg: str = "ES256", kid: str | None = None) -> str:
    """A correctly signed token — the fixture's own signer, verified by ``jose``."""

    if alg == "ES256":
        key_id = kid or ES_KID
        header = {"alg": "ES256", "typ": "JWT", "kid": key_id}
        signing_input = f"{jose.b64url_encode(json.dumps(header, sort_keys=True).encode())}.{jose.b64url_encode(json.dumps(claims, sort_keys=True).encode())}"
        digest = hashlib.sha256(signing_input.encode()).digest()
        nonce = int.from_bytes(digest, "big") % jose._N
        if nonce == 0:
            nonce = 1
        signature = jose.sign_es256(ES_SECRET, signing_input.encode(), k=nonce)
    elif alg == "RS256":
        key_id = kid or RSA_KID
        header = {"alg": "RS256", "typ": "JWT", "kid": key_id}
        signing_input = f"{jose.b64url_encode(json.dumps(header, sort_keys=True).encode())}.{jose.b64url_encode(json.dumps(claims, sort_keys=True).encode())}"
        size = (RSA_N.bit_length() + 7) // 8
        tail = jose._SHA256_DIGEST_INFO + hashlib.sha256(signing_input.encode()).digest()
        em = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
        signature = pow(int.from_bytes(em, "big"), RSA_D, RSA_N).to_bytes(size, "big")
    else:  # pragma: no cover - fixtures only use the two asymmetric algorithms
        raise ValueError("unsupported fixture algorithm")
    return f"{signing_input}.{jose.b64url_encode(signature)}"


def claims_for(user_id: str = ADMIN_ID, **overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "name": "Коммунар Тестовый",
        "preferred_username": "commune_test",
    }
    claims.update(overrides)
    return claims


class FakeOidcProvider:
    """An in-process OIDC provider: discovery, JWKS and a token endpoint."""

    def __init__(self, *, user_id: str = ADMIN_ID, claims: Mapping[str, Any] | None = None, alg: str = "ES256") -> None:
        self.user_id = user_id
        self.alg = alg
        self.claims = dict(claims) if claims is not None else None
        self.requests: list[tuple[str, str]] = []
        self.token_status = 200
        self.jwks_document: dict[str, Any] | None = None
        self.issuer = ISSUER
        self.code_verifier_seen: str | None = None
        self.state_seen: str | None = None
        self.nonce: str | None = None
        self.jwks_responses: list[dict[str, Any]] = []

    # transport signature: (method, url, *, data, headers) -> (status, body)
    def __call__(self, method: str, url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None, **_kw: Any) -> tuple[int, bytes]:
        self.requests.append((method, url))
        if url.endswith("/.well-known/openid-configuration"):
            return 200, json.dumps(
                {
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "id_token_signing_alg_values_supported": [self.alg],
                }
            ).encode()
        if url.endswith("/jwks"):
            if self.jwks_responses:
                document = self.jwks_responses.pop(0) if len(self.jwks_responses) > 1 else self.jwks_responses[0]
                return 200, json.dumps(document).encode()
            if self.jwks_document is not None:
                return 200, json.dumps(self.jwks_document).encode()
            key = es_jwk() if self.alg == "ES256" else rs_jwk()
            return 200, json.dumps({"keys": [key]}).encode()
        if url.endswith("/token"):
            if self.token_status != 200:
                return self.token_status, b"{}"
            form = parse_qs((data or b"").decode())
            self.code_verifier_seen = (form.get("code_verifier") or [None])[0]
            claims = dict(self.claims if self.claims is not None else claims_for(self.user_id))
            if self.nonce is not None:
                claims["nonce"] = self.nonce
            token = sign_jwt(claims, alg=self.alg)
            return 200, json.dumps({"id_token": token, "access_token": "synthetic", "token_type": "Bearer"}).encode()
        return 404, b"{}"


def login(http: "WsgiClient", provider: FakeOidcProvider, *, code: str = "synthetic-code", path: str = "/admin/callback") -> "ClientResponse":
    """Walk the whole OIDC login: begin, echo the nonce, exchange, redirect."""

    start = http.request("GET", "/admin/login")
    start.absorb_cookies()
    if start.location is None or "state=" not in start.location:
        return start
    query = parse_qs(start.location.split("?", 1)[1])
    provider.nonce = (query.get("nonce") or [None])[0]
    response = http.request("GET", f"{path}?code={code}&state={query['state'][0]}")
    response.absorb_cookies()
    return response


def csrf_from(response: "ClientResponse") -> str:
    """The CSRF token of the desk form on a page (the same value the browser sees)."""

    import re

    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    if match is None:
        raise AssertionError("no CSRF token on the page")
    return match.group(1)


def oidc_client(transport: FakeOidcProvider, **overrides: Any) -> oidc.TelegramOidc:
    options: dict[str, Any] = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "secret": SERVER_SECRET,
        "transport": transport,
        "allowed_redirect_uris": (REDIRECT_URI,),
    }
    options.update(overrides)
    return oidc.TelegramOidc(**options)


def widget_payload(user_id: str = ADMIN_ID, *, username: str | None = None, auth_date: str | None = None) -> dict[str, Any]:
    stamp = auth_date or str(int(datetime.now(timezone.utc).timestamp()))
    return oidc.widget_material(user_id, bot_token=BOT_TOKEN, auth_date=stamp, username=username)


# --- HTTP client over WSGI ------------------------------------------------- #

class WsgiClient:
    """A minimal cookie-keeping WSGI client; no server, no sockets."""

    def __init__(self, app: Callable[..., Any], *, host: str = "desk.example", https: bool = True) -> None:
        self.app = app
        self.host = host
        self.scheme = "https" if https else "http"
        self.cookies: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        follow: bool = False,
    ) -> "ClientResponse":
        query = ""
        if "?" in path:
            path, _, query = path.partition("?")
        body = b""
        extra = dict(headers or {})
        if form is not None:
            body = urlencode(dict(form)).encode()
            extra.setdefault("CONTENT_TYPE", "application/x-www-form-urlencoded")
            extra.setdefault("CONTENT_LENGTH", str(len(body)))
        cookie_header = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": self.host,
            "SERVER_PORT": "443" if self.scheme == "https" else "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "CONTENT_TYPE": "",
            "CONTENT_LENGTH": "",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": self.scheme,
            "wsgi.input": _Stream(body),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        if cookie_header:
            environ["HTTP_COOKIE"] = cookie_header
        environ["HTTP_HOST"] = self.host
        for name, value in extra.items():
            upper = name.upper()
            if upper in ("CONTENT_TYPE", "CONTENT_LENGTH", "REQUEST_METHOD", "PATH_INFO", "QUERY_STRING"):
                environ[upper] = value
            elif upper.startswith("WSGI."):
                environ[name] = value
            else:
                environ[f"HTTP_{upper}"] = value
        status_holder: dict[str, Any] = {}

        def start_response(status: str, response_headers: list[tuple[str, str]], _exc: Any = None) -> Callable[[bytes], None]:
            status_holder["status"] = status
            status_holder["headers"] = response_headers
            return lambda _data: None

        chunks = self.app(environ, start_response)
        payload = b"".join(chunks)
        response = ClientResponse(int(str(status_holder["status"]).split()[0]), status_holder["headers"], payload)
        response._client = self
        if follow and response.status in (301, 302, 303, 307, 308) and response.location:
            return self.request("GET", response.location, follow=True)
        return response


class _Stream:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self.data[self.position:]
            self.position = len(self.data)
            return chunk
        chunk = self.data[self.position:self.position + size]
        self.position += len(chunk)
        return chunk


class ClientResponse:
    def __init__(self, status: int, headers: Sequence[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = [(name, value) for name, value in headers]
        self.body = body
        self._client: WsgiClient | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    @property
    def location(self) -> str | None:
        return self.header("Location")

    def set_cookie(self, name: str, value: str) -> None:
        if self._client is not None:
            self._client.cookies[name] = value

    def absorb_cookies(self) -> dict[str, str]:
        """Apply every Set-Cookie of this response to the client jar."""

        for key, value in self.headers:
            if key.lower() != "set-cookie":
                continue
            pair = value.split(";", 1)[0]
            name, _, cookie_value = pair.partition("=")
            if self._client is not None:
                if cookie_value == "" or "max-age=0" in value.lower():
                    self._client.cookies.pop(name, None)
                else:
                    self._client.cookies[name] = cookie_value
        return dict(self._client.cookies) if self._client else {}


# --- database -------------------------------------------------------------- #

def build_pipeline_db(path: str | Path, *, release_dir: str | Path | None = None, period: str = "2025-07") -> tuple[Any, Path]:
    """Run the real nightly cycle from offline fixtures into a file database."""

    from tests.dev_release import build as build_release

    target = Path(path)
    releases = Path(release_dir) if release_dir else target.parent / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    build_release(str(releases), period=period, db_path=str(target))
    conn = connect(str(target))
    apply_migrations(conn)
    return conn, releases


def record_mappings(conn: Any) -> None:
    """Persist the synthetic public contour and calendar mapping in the store."""

    policy = fx.policy()
    repo.record_category_mapping(
        conn,
        version="policy-synthetic-1",
        payload={
            "public_accounts": sorted(policy.public_accounts),
            "private_accounts": sorted(policy.private_accounts),
            "quarantined_accounts": sorted(policy.quarantined_accounts),
            "category_classes": {key: str(value) for key, value in policy.category_classes.items()},
            "category_labels": dict(fx.CATEGORY_LABELS),
            "quarantined_categories": sorted(policy.quarantined_categories),
            "commission_categories": sorted(policy.commission_categories),
            "subsidy_category_ids": sorted(policy.subsidy_category_ids),
            "count_visibility_threshold_kopecks": policy.count_visibility_threshold_kopecks,
            "sensitive_categories": sorted(policy.sensitive_categories),
            "unclassified_label": policy.unclassified_label,
        },
    )
    mapping = fx.event_mapping()
    repo.record_event_mapping(
        conn,
        version="event-map-synthetic-1",
        payload={
            "subcalendar_types": {key: str(value) for key, value in mapping.subcalendar_types.items()},
            "private_subcalendars": sorted(mapping.private_subcalendars),
        },
    )


def _balance(account_id: str, as_of: str, kopecks: int) -> Any:
    from randreport.domain.flows import AccountScope
    from randreport.domain.finance import AccountBalance

    return AccountBalance(
        account_id=account_id,
        currency="RUB",
        balance_kopecks=kopecks,
        as_of=date.fromisoformat(as_of),
        is_liquid=True,
        confirmed=True,
        scope=AccountScope.PUBLIC,
    )


def add_balances(conn: Any) -> None:
    repo.upsert_balance_snapshots(
        conn,
        [
            _balance("acc_card", "2025-07-01", 5_000_000),
            _balance("acc_cash", "2025-07-01", 500_000),
            _balance("acc_card", "2025-07-31", 5_600_000),
            _balance("acc_cash", "2025-07-31", 600_000),
        ],
        source="zenmoney",
    )


def add_balance_snapshot_stub(conn: Any, *, as_of: str, kopecks: int) -> None:
    repo.upsert_balance_snapshots(conn, [_balance("acc_card", as_of, kopecks)], source="zenmoney")


def extra_news(item: Any) -> Any:
    return item


def news_items_from(conn: Any) -> tuple[dict[str, Any], ...]:
    return tuple(dict(row) for row in conn.execute("SELECT * FROM news_items ORDER BY news_id"))


def workspace(tmp: Path) -> tuple[Any, Path]:
    """A ready staging database with mappings, balances and history."""

    from randreport.admin import history_admin

    conn, releases = build_pipeline_db(tmp / "randreport.sqlite")
    record_mappings(conn)
    add_balances(conn)
    history_admin.ingest(conn)
    history_admin.detect_conflicts(conn)
    return conn, releases


def _template_root() -> Path:
    """One staging build per test process; each test gets a private copy."""

    global _TEMPLATE
    if _TEMPLATE is None:
        root = Path(tempfile.mkdtemp(prefix="randreport-admin-template-"))
        conn, _releases = workspace(root)
        conn.close()  # the template is a directory; the connection is not needed afterwards
        _TEMPLATE = root
    return _TEMPLATE


_TEMPLATE: Path | None = None


def admin_case_dir(prefix: str = "randreport-admin-") -> Path:
    """A private copy of the staging build for one test."""

    target = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copytree(_template_root(), target, dirs_exist_ok=True, symlinks=True)
    return target
