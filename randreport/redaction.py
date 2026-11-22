"""Redaction of secrets before anything reaches a log, an error message or a CLI.

The nightly cycle, the connectors and the transport all build strings that may
carry a credential: a request URL with ``?token=…``, an ``Authorization`` header,
a Telegram bot path ``/bot<id>:<secret>/…``, or a query string on an error page.
Writing any of those into a structured log is an operational leak
(docs/REQUIREMENTS.md section 9: "Логи структурированные, без платёжных данных,
токенов, содержимого закрытых событий").

This module is deliberately dependency-free and pure: :func:`redact` never raises
and never needs the value it is hiding to be recognised as a real secret, so it is
safe to call on every untrusted string (a URL, a header map, a whole traceback
line) before it is printed. It removes *values*, never the shape of the message,
so an operator still sees which parameter was present.
"""

from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "MASK",
    "SECRET_PARAM_NAMES",
    "redact",
    "redact_url",
    "redact_headers",
    "redact_mapping",
]

MASK = "***"

#: Query/body parameter names whose value is a credential. Matched
#: case-insensitively as a whole word so ``token`` also hides ``access_token``.
SECRET_PARAM_NAMES = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "bot_token",
        "api_key",
        "apikey",
        "key",
        "calendar_key",
        "secret",
        "client_secret",
        "password",
        "passphrase",
        "signature",
        "sig",
        "code",
        "auth",
        "authorization",
    }
)

# ``key=value`` / ``"key": "value"`` / ``key: value`` in a URL, a JSON snippet or
# a header line. The value stops at a delimiter or whitespace.
_PARAM_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.\-]*?(?:token|key|secret|password|passphrase|signature|sig|authorization|auth|code))"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s&,;\"'<>]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)

# ``Authorization: Bearer <blob>`` and the bare ``Bearer <blob>`` form.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=\-]{4,}")

# A Telegram bot token path segment: /bot8123456:AAF-secretsecretsecret/
_TELEGRAM_BOT_RE = re.compile(r"/bot[0-9]{5,}:[A-Za-z0-9_\-]{10,}")

# A long opaque run that looks like a credential even without a parameter name
# (a calendar key, an OAuth token). Requires mixed case or a digit to avoid
# eating ordinary words and hex/epoch numbers.
_OPAQUE_RE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{24,}(?![A-Za-z0-9_\-])")


def _looks_like_secret(token: str) -> bool:
    has_digit = any(ch.isdigit() for ch in token)
    has_alpha = any(ch.isalpha() for ch in token)
    has_upper = any(ch.isupper() for ch in token)
    has_lower = any(ch.islower() for ch in token)
    return has_digit and has_alpha and has_upper and has_lower


def redact_url(url: str) -> str:
    """Drop the query string and any userinfo from a URL, keeping its shape.

    The query of an ``https://api…/v8/diff/?token=…`` request is where a secret
    travels; the host and path are what an operator needs to see.
    """

    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit is very permissive
        return MASK
    if not parts.scheme:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        netloc = f"{MASK}@{netloc}"
    tail = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    if parts.query:
        tail = tail + "?" + MASK
    return tail


def redact(text: str) -> str:
    """Return ``text`` with every recognisable secret value masked.

    Pure and total: a non-string input is stringified, an unmatchable string is
    returned unchanged.
    """

    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    text = _TELEGRAM_BOT_RE.sub(f"/bot{MASK}", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _PARAM_RE.sub(lambda m: f"{m.group('key')}{m.group('sep')}{m.group('quote')}{MASK}{m.group('quote')}", text)
    text = _OPAQUE_RE.sub(lambda m: MASK if _looks_like_secret(m.group(0)) else m.group(0), text)
    return text


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``headers`` whose credential-bearing values are masked."""

    out: dict[str, str] = {}
    for name, value in headers.items():
        if str(name).lower() in ("authorization", "cookie", "proxy-authorization", "x-api-key", "x-auth-token"):
            out[str(name)] = MASK
        else:
            out[str(name)] = redact(str(value))
    return out


def redact_mapping(values: Mapping[str, object]) -> dict[str, object]:
    """Mask the values of credential-shaped keys in a request/response record."""

    out: dict[str, object] = {}
    for name, value in values.items():
        if str(name).lower() in SECRET_PARAM_NAMES:
            out[str(name)] = MASK
        elif isinstance(value, Mapping):
            out[str(name)] = redact_mapping(value)
        elif isinstance(value, str):
            out[str(name)] = redact(value)
        else:
            out[str(name)] = value
    return out
