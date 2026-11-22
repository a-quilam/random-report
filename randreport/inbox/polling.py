"""``getUpdates`` polling intake for public channel posts.

The ``docs/REQUIREMENTS.md`` section 8 intake is an *event* intake, not history:
either a webhook delivery or a poll writes each incoming
``channel_post`` / ``edited_channel_post`` durably into the private inbox
(``telegram_inbox``) and the 03:00 cycle does all processing.  A webhook needs a
public TLS endpoint; without a domain the same intake runs as a short
``getUpdates`` poll, which is what this module implements.

The rules the card fixes, enforced here:

* **the webhook wins.**  ``getUpdates`` and a registered webhook are mutually
  exclusive in the Bot API; a poll would either conflict (``409``) or steal
  another consumer's updates.  The poll therefore reads ``getWebhookInfo`` first
  and **refuses to run while any webhook is registered** — and it never calls
  ``setWebhook``/``deleteWebhook``/``drop_pending_updates``.  An existing
  consumer is left exactly as it was found;
* **only the allowed channel, only posts and edits.**  Every returned update is
  parsed with the same parser the webhook uses; a non-post update is ignored, an
  update from another chat is refused, and only an allowed
  ``channel_post``/``edited_channel_post`` is stored;
* **durable offset only after the write.**  The rows are committed to the inbox
  first; the offset (``max(update_id) + 1``) is persisted in a second
  transaction.  A crash between the two re-reads the same updates, which the
  inbox deduplicates by ``update_id`` — nothing is lost and nothing is doubled.

This module never guesses: a missing token or a refused poll is an error, never a
silently empty intake.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..connectors.transport import HttpStatusError, Request, Response, Transport, TransportError
from ..store.db import transaction
from .store import InboxPost, accept_post
from .webhook import POST_KINDS, ForeignChannel, IncomingUpdate, UpdateRejected, parse_update

DEFAULT_BASE_URL = "https://api.telegram.org"
#: where the durable offset lives in the private store (``telegram_poll_state``)
OFFSET_KEY = "getupdates_offset"
MAX_LIMIT = 100


class PollingError(RuntimeError):
    """A poll that must not run (no token, a webhook is active, a bad response)."""


class WebhookActive(PollingError):
    """A webhook is registered: ``getUpdates`` must not be used.

    The card forbids clearing someone else's webhook or queue, so the poll stops
    here and reports the state instead of calling ``deleteWebhook``.
    """


@dataclass(frozen=True)
class WebhookState:
    """What ``getWebhookInfo`` said, reduced to what matters for the decision."""

    url_set: bool
    pending_update_count: int
    last_error_message: str | None = None
    allowed_updates: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "url_set": self.url_set,
            "pending_update_count": self.pending_update_count,
            "last_error_message": self.last_error_message,
            "allowed_updates": list(self.allowed_updates) if self.allowed_updates is not None else None,
        }


@dataclass(frozen=True)
class PollReport:
    """Counters for one poll; it never carries a post body or an identifier."""

    polled: bool
    webhook: WebhookState
    received: int = 0
    accepted: int = 0
    revised: int = 0
    duplicate: int = 0
    ignored: int = 0
    foreign: int = 0
    rejected: int = 0
    offset: int | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "polled": self.polled,
            "webhook": self.webhook.as_dict(),
            "received": self.received,
            "accepted": self.accepted,
            "revised": self.revised,
            "duplicate": self.duplicate,
            "ignored": self.ignored,
            "foreign": self.foreign,
            "rejected": self.rejected,
            "offset": self.offset,
        }
        if self.note:
            payload["note"] = self.note
        return payload


def _api_url(base_url: str, token: str, method: str) -> str:
    return f"{base_url.rstrip('/')}/bot{token}/{method}"


def _call(transport: Transport, request: Request) -> Mapping[str, Any]:
    try:
        response: Response = transport.send(request)
    except HttpStatusError as exc:
        raise PollingError(f"Telegram answered HTTP {exc.status}") from exc
    except TransportError as exc:
        raise PollingError(f"the Bot API is unreachable: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise PollingError("the Bot API answer is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise PollingError("the Bot API answer is not an object")
    if not payload.get("ok"):
        # a refused call (for example a 409 because a webhook is active)
        raise PollingError(f"the Bot API refused the call: {payload.get('description') or 'unknown error'}")
    return payload


def read_webhook_info(
    transport: Transport, *, bot_token: str | None, base_url: str = DEFAULT_BASE_URL
) -> WebhookState:
    """``getWebhookInfo`` as a decision input; never a mutation."""

    if not bot_token:
        raise PollingError("TELEGRAM_BOT_TOKEN is required to poll for updates")
    payload = _call(
        transport,
        Request(method="GET", url=_api_url(base_url, bot_token, "getWebhookInfo")),
    )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise PollingError("getWebhookInfo returned no result")
    allowed = result.get("allowed_updates")
    return WebhookState(
        url_set=bool(str(result.get("url") or "").strip()),
        pending_update_count=int(result.get("pending_update_count") or 0),
        last_error_message=(str(result["last_error_message"]) if result.get("last_error_message") else None),
        allowed_updates=tuple(str(item) for item in allowed) if isinstance(allowed, list) else None,
    )


def fetch_updates(
    transport: Transport,
    *,
    bot_token: str | None,
    base_url: str = DEFAULT_BASE_URL,
    offset: int | None = None,
    limit: int = MAX_LIMIT,
    timeout_s: float = 20.0,
) -> tuple[Mapping[str, Any], ...]:
    """One ``getUpdates`` call, restricted to posts and edits.

    ``offset`` is passed only when a durable offset already exists; without one
    the pending updates are read *without* confirming them, so a first poll that
    fails before the write loses nothing.
    """

    if not bot_token:
        raise PollingError("TELEGRAM_BOT_TOKEN is required to poll for updates")
    params = {
        "limit": str(max(1, min(int(limit), MAX_LIMIT))),
        "timeout": "0",
        "allowed_updates": json.dumps(list(POST_KINDS)),
    }
    if offset is not None:
        params["offset"] = str(int(offset))
    payload = _call(
        transport,
        Request(
            method="GET",
            url=_api_url(base_url, bot_token, "getUpdates"),
            params=params,
            timeout_s=timeout_s,
        ),
    )
    result = payload.get("result")
    if not isinstance(result, list):
        raise PollingError("getUpdates returned no list of updates")
    return tuple(item for item in result if isinstance(item, Mapping))


def stored_offset(conn: sqlite3.Connection) -> int | None:
    """The durable ``getUpdates`` offset, or ``None`` when none was persisted yet."""

    row = conn.execute("SELECT value FROM telegram_poll_state WHERE key = ?", (OFFSET_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = int(row["value"])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def save_offset(conn: sqlite3.Connection, offset: int, *, now: datetime | None = None) -> None:
    """Persist the offset; separate from the inbox write on purpose."""

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with transaction(conn):
        conn.execute(
            "INSERT INTO telegram_poll_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (OFFSET_KEY, str(int(offset)), stamp),
        )


def _channel_key(update: IncomingUpdate, allowed: frozenset[str]) -> str | None:
    if update.channel_id in allowed:
        return update.channel_id
    bare = update.channel_id.lstrip("@")
    for candidate in sorted(allowed):
        if candidate.lstrip("@") == bare:
            return candidate
    return None


def _update_id(update: Mapping[str, Any]) -> int | None:
    value = update.get("update_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def ingest_updates(
    conn: sqlite3.Connection,
    updates: Sequence[Mapping[str, Any]],
    *,
    allowed_channels: Iterable[str],
    now: datetime | None = None,
) -> tuple[dict[str, int], int | None]:
    """Store the allowed posts; return counters and the highest ``update_id`` seen."""

    allowed = frozenset(str(channel) for channel in allowed_channels)
    counts = {"received": 0, "accepted": 0, "revised": 0, "duplicate": 0, "ignored": 0, "foreign": 0, "rejected": 0}
    highest: int | None = None
    received_at = now or datetime.now(timezone.utc)
    for update in updates:
        counts["received"] += 1
        update_id = _update_id(update)
        if update_id is not None and (highest is None or update_id > highest):
            highest = update_id
        try:
            parsed = parse_update(update)
        except ForeignChannel:
            counts["foreign"] += 1
            continue
        except UpdateRejected:
            # an unparseable update must not block the queue forever; it is
            # counted and skipped, never stored
            counts["rejected"] += 1
            continue
        if parsed is None:
            counts["ignored"] += 1
            continue
        channel_key = _channel_key(parsed, allowed)
        if channel_key is None:
            counts["foreign"] += 1
            continue
        outcome = accept_post(conn, parsed.as_post(channel_key=channel_key, received_at=received_at))
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts, highest


def poll_into_inbox(
    conn: sqlite3.Connection,
    transport: Transport,
    *,
    bot_token: str | None,
    allowed_channels: Iterable[str],
    base_url: str = DEFAULT_BASE_URL,
    limit: int = MAX_LIMIT,
    now: datetime | None = None,
    require_no_webhook: bool = True,
) -> PollReport:
    """Read the pending updates once and store the allowed posts durably."""

    webhook = read_webhook_info(transport, bot_token=bot_token, base_url=base_url)
    if require_no_webhook and webhook.url_set:
        # never delete someone else's webhook: report and stop
        raise WebhookActive(
            "a webhook is registered for this bot; getUpdates would conflict with it and the webhook is not removed"
        )
    offset = stored_offset(conn)
    updates = fetch_updates(
        transport,
        bot_token=bot_token,
        base_url=base_url,
        offset=offset,
        limit=limit,
        timeout_s=20.0,
    )
    counts, highest = ingest_updates(conn, updates, allowed_channels=allowed_channels, now=now)
    new_offset: int | None = offset
    if highest is not None:
        # the inbox rows are already committed (accept_post commits per row);
        # only now, after the write, is the offset advanced
        new_offset = highest + 1
        if offset is None or new_offset > offset:
            save_offset(conn, new_offset, now=now)
        else:  # pragma: no cover - a requeued older update
            new_offset = offset
    return PollReport(
        polled=True,
        webhook=webhook,
        received=counts["received"],
        accepted=counts["accepted"],
        revised=counts["revised"],
        duplicate=counts["duplicate"],
        ignored=counts["ignored"],
        foreign=counts["foreign"],
        rejected=counts["rejected"],
        offset=new_offset,
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "MAX_LIMIT",
    "OFFSET_KEY",
    "PollingError",
    "PollReport",
    "WebhookActive",
    "WebhookState",
    "fetch_updates",
    "ingest_updates",
    "poll_into_inbox",
    "read_webhook_info",
    "save_offset",
    "stored_offset",
]
