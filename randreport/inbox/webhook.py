"""The webhook side of the inbox: a small WSGI application, nothing outbound.

Design rules taken from ``docs/REQUIREMENTS.md`` sections 8 and 11:

* **the secret is required.**  Telegram sends it in the
  ``X-Telegram-Bot-Api-Secret-Token`` header; the application refuses to start
  without one and answers ``401`` on a missing or wrong value (constant-time
  comparison).  There is no unauthenticated write path;
* **the channel is allow-listed.**  An update for any other chat is refused with
  ``403`` and is not stored; an empty allowlist denies everything rather than
  allowing everything;
* **the body is bounded.**  A body above the limit is refused with ``413`` before
  it is parsed, so a hostile sender cannot buy memory with a large request;
* **intake is durable and idempotent.**  A ``channel_post`` or
  ``edited_channel_post`` is written to ``telegram_inbox`` in one transaction and
  only then answered with ``200``.  A replay (Telegram retries until it sees a
  2xx) answers ``200`` and writes nothing;
* **anything else is ignored, not failed.**  An update the application does not
  care about (a bot message, a callback) answers ``200`` with ``ignored`` —
  Telegram must not keep retrying something we will never store.  A malformed
  *interesting* update is ``400`` (a programming/config problem worth surfacing);
* **back-pressure is explicit.**  Above the configured rate the application
  answers ``429`` with ``Retry-After``; Telegram re-delivers later.  That is
  back-pressure, not data loss.

This module performs no HTTP request of its own: no ``setWebhook`` call and no
external service is ever contacted (the card forbids it, and so does section 8 —
the webhook is served, never registered).
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..store import apply_migrations, connect
from .store import InboxPost, accept_post

WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"
DEFAULT_MAX_BODY_BYTES = 16_384
DEFAULT_MAX_UPDATES_PER_MINUTE = 120
#: update keys the inbox stores; everything else is acknowledged and ignored
POST_KINDS = ("channel_post", "edited_channel_post")

STATUS_TEXT = {
    200: "200 OK",
    400: "400 Bad Request",
    401: "401 Unauthorized",
    403: "403 Forbidden",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    413: "413 Content Too Large",
    429: "429 Too Many Requests",
    500: "500 Internal Server Error",
}


class WebhookConfigError(ValueError):
    """The application is not safe to serve (for example: no secret configured)."""


class UpdateRejected(ValueError):
    """The body is not an update this application can store (``400``)."""


class ForeignChannel(UpdateRejected):
    """The update belongs to a chat outside the allowlist (``403``, never stored)."""


@dataclass(frozen=True)
class IncomingUpdate:
    """A parsed ``channel_post`` / ``edited_channel_post``."""

    update_id: str
    kind: str
    channel_id: str
    message_id: str
    posted_at: datetime
    text: str
    edited_at: datetime | None = None
    media_group_id: str | None = None

    def as_post(self, *, channel_key: str, received_at: datetime | None = None) -> InboxPost:
        return InboxPost(
            update_id=self.update_id,
            kind=self.kind,
            channel_id=channel_key,
            message_id=self.message_id,
            posted_at=self.posted_at,
            text=self.text,
            edited_at=self.edited_at,
            media_group_id=self.media_group_id,
            received_at=received_at or datetime.now(timezone.utc),
        )


def _moment(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpdateRejected(f"{field_name} must be a unix timestamp")
    if value <= 0:
        raise UpdateRejected(f"{field_name} must be positive")
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def parse_update(payload: Any) -> IncomingUpdate | None:
    """Parse one update; return ``None`` when it is not a public channel post."""

    if not isinstance(payload, Mapping):
        raise UpdateRejected("an update must be a JSON object")
    kind = next((name for name in POST_KINDS if name in payload), None)
    if kind is None:
        return None
    body = payload[kind]
    if not isinstance(body, Mapping):
        raise UpdateRejected(f"{kind} must be an object")
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id <= 0:
        raise UpdateRejected("update_id must be a positive integer")
    chat = body.get("chat")
    if not isinstance(chat, Mapping):
        raise UpdateRejected("the post has no chat")
    chat_id = chat.get("id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise UpdateRejected("chat.id must be an integer")
    message_id = body.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise UpdateRejected("message_id must be a positive integer")
    text = body.get("text")
    if text is None:
        # a photo-only post has a caption; a post with neither carries no
        # public text to summarise, so it is acknowledged and ignored
        text = body.get("caption")
    if not isinstance(text, str) or not text.strip():
        raise UpdateRejected("the post has no text to archive")
    edited = None
    if kind == "edited_channel_post":
        edited = _moment(body.get("edit_date") or body.get("date"), field_name="edit_date")
    channel_id = f"@{chat['username']}" if chat.get("username") else str(chat_id)
    return IncomingUpdate(
        update_id=str(update_id),
        kind=kind,
        channel_id=str(channel_id),
        message_id=str(message_id),
        posted_at=_moment(body.get("date"), field_name="date"),
        text=text,
        edited_at=edited,
        media_group_id=None if body.get("media_group_id") is None else str(body["media_group_id"]),
    )


class _RateWindow:
    """A sliding window of accepted events; ``None`` disables the limit."""

    def __init__(self, per_minute: int | None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: list[float] = []

    def allow(self) -> bool:
        if self._limit is None:
            return True
        now = self._clock()
        with self._lock:
            self._seen = [moment for moment in self._seen if now - moment < 60.0]
            if len(self._seen) >= self._limit:
                return False
            self._seen.append(now)
        return True


class TelegramWebhook:
    """WSGI application serving ``POST /telegram/webhook``.

    ``conn`` is used directly when given (an in-memory test database); otherwise
    each request opens and closes its own connection, so a long-lived server
    never holds a handle open between requests.  An injected connection is used
    from whatever worker thread the WSGI server picks, so it must be created with
    ``check_same_thread=False``.
    """

    def __init__(
        self,
        *,
        secret_token: str | None,
        allowed_channels: Iterable[str] = (),
        db_path: str | Path | None = None,
        conn: sqlite3.Connection | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_updates_per_minute: int | None = DEFAULT_MAX_UPDATES_PER_MINUTE,
        path: str = WEBHOOK_PATH,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not secret_token or len(secret_token) < 16:
            raise WebhookConfigError(
                "a webhook secret of at least 16 characters is required; the application is not served without it"
            )
        if conn is None and db_path is None:
            raise WebhookConfigError("either conn or db_path is required")
        self._secret = secret_token
        self._allowed = frozenset(str(channel) for channel in allowed_channels)
        self._conn = conn
        self._db_path = None if conn is not None else Path(str(db_path))
        self._max_body = max_body_bytes
        self._path = path
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._window = _RateWindow(max_updates_per_minute, clock=clock)
        if self._conn is not None:
            apply_migrations(self._conn)

    # --- connection handling ---------------------------------------------- #

    @property
    def path(self) -> str:
        return self._path

    @property
    def allowed_channels(self) -> frozenset[str]:
        """The allowlist this application serves; empty means "deny everything"."""

        return self._allowed

    def _open(self):
        if self._conn is not None:
            return self._conn, False
        path = self._db_path
        if path is None:  # pragma: no cover - __init__ refuses this combination
            raise WebhookConfigError("no database configured")
        conn = connect(path)
        apply_migrations(conn)
        return conn, True

    # --- WSGI ------------------------------------------------------------- #

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        status, payload, extra = self._dispatch(environ)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store"), *extra]
        start_response(STATUS_TEXT.get(status, str(status)), headers)
        return [body]

    # --- request handling -------------------------------------------------- #

    def _dispatch(self, environ: Mapping[str, Any]) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
        if str(environ.get("PATH_INFO", "")) != self._path:
            return 404, {"ok": False, "error": "not_found"}, []
        if str(environ.get("REQUEST_METHOD", "GET")).upper() != "POST":
            return 405, {"ok": False, "error": "method_not_allowed"}, []
        present = environ.get(SECRET_HEADER)
        if not isinstance(present, str) or not hmac.compare_digest(present, self._secret):
            # the secret is never echoed, logged or compared with ``==``
            return 401, {"ok": False, "error": "bad_secret"}, []

        raw = self._read_body(environ)
        if raw is None:
            return 413, {"ok": False, "error": "body_too_large"}, []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"ok": False, "error": "invalid_json"}, []
        try:
            update = parse_update(payload)
        except UpdateRejected as exc:
            return 400, {"ok": False, "error": "invalid_update", "reason": str(exc)}, []
        if update is None:
            return 200, {"ok": True, "status": "ignored"}, []

        channel_id = self._channel_key(update)
        if channel_id is None:
            return 403, {"ok": False, "error": "channel_not_allowed"}, []
        if not self._window.allow():
            return 429, {"ok": False, "error": "rate_limited"}, [("Retry-After", "60")]

        conn, owned = self._open()
        try:
            outcome = accept_post(
                conn, update.as_post(channel_key=channel_id, received_at=self._now())
            )
        finally:
            if owned:
                conn.close()
        return 200, {"ok": True, "status": outcome.status, "message_id": outcome.message_id}, []

    def _channel_key(self, update: IncomingUpdate) -> str | None:
        """Map the update's chat onto the allowlist; ``None`` means "refuse"."""

        if update.channel_id in self._allowed:
            return update.channel_id
        # the same chat can be addressed by username or numeric id
        bare = update.channel_id.lstrip("@")
        for allowed in sorted(self._allowed):
            if allowed.lstrip("@") == bare:
                return allowed
        return None

    def _read_body(self, environ: Mapping[str, Any]) -> bytes | None:
        """The request body, or ``None`` for anything above the configured limit.

        The declared length is checked *before* a byte is read, and only that many
        bytes are read: a WSGI input stream blocks if asked for more than the
        request carries, and a hostile sender must not be able to buy memory.
        """

        try:
            length = int(str(environ.get("CONTENT_LENGTH") or "0").strip() or "0")
        except ValueError:
            return b""
        if length < 0 or length > self._max_body:
            return None
        if length == 0:
            return b""
        stream = environ.get("wsgi.input")
        if stream is None:
            return b""
        data = stream.read(length)
        return data if isinstance(data, bytes) else b""

    # --- lifecycle --------------------------------------------------------- #

    def close(self) -> None:
        """Close the injected connection, if this application owns it."""

        if self._conn is not None:
            self._conn.close()
            self._conn = None
