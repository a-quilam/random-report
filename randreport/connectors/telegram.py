"""Telegram read-only client: one daily subscriber snapshot and post history.

Two separate concerns are kept apart on purpose (docs/REQUIREMENTS.md section 8):

* the subscriber count is read once a day from the official Bot API
  ``getChatMemberCount``; an API error is an error, never a zero;
* the post history is read through a read-only history source (an MTProto client
  limited to the one public channel, or a passive webhook inbox). The Bot API
  ``getUpdates`` queue is explicitly *not* used for history: it keeps updates for
  at most 24 hours, so a single failed day would lose data.

Reading anything but the configured public channel is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Protocol, Sequence

from .transport import HttpStatusError, Request, Response, Transport, TransportError

DEFAULT_BASE_URL = "https://api.telegram.org"
TELEGRAM_SOURCE = "telegram"


class TelegramError(RuntimeError):
    """An API error or an attempt to read a channel that is not allowed."""


class TelegramProtocolError(RuntimeError):
    """A response that does not match the documented Bot API schema."""


class HistoryUnavailable(TelegramError):
    """A history source that needs real credentials or an approved session."""


@dataclass(frozen=True)
class RawPost:
    message_id: str
    channel_id: str
    posted_at: datetime
    text: str
    edited_at: datetime | None = None
    revision: int = 1
    deleted: bool = False
    media_album_id: str | None = None

    def __post_init__(self) -> None:
        if self.posted_at.tzinfo is None:
            raise ValueError("posted_at must be timezone aware")


@dataclass(frozen=True)
class ChannelSubscriberPoint:
    on: date
    subscribers: int
    source: str = TELEGRAM_SOURCE
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PostSource(Protocol):
    """Read-only history source for one public channel."""

    def fetch(self, *, since_id: str | None, limit: int) -> Sequence[RawPost]:  # pragma: no cover - protocol
        ...


class StaticPostSource:
    """Offline source used by tests and by the passive webhook inbox."""

    def __init__(self, posts: Iterable[RawPost]) -> None:
        self._posts = sorted(posts, key=lambda post: (post.posted_at, post.message_id))

    def fetch(self, *, since_id: str | None, limit: int) -> Sequence[RawPost]:
        ordered = list(self._posts)
        if since_id is not None:
            index = next((i for i, post in enumerate(ordered) if post.message_id == since_id), None)
            if index is not None:
                ordered = ordered[index + 1 :]
        return tuple(ordered[: max(limit, 0)])


class MTProtoHistorySource:
    """Placeholder for the approved read-only MTProto history reader.

    It requires a real, separately authorised session (secret store) and is
    therefore reported as a blocked integration, not faked here.
    """

    def __init__(self, *, allowed_channel_ids: Iterable[str]) -> None:
        self.allowed_channel_ids = frozenset(allowed_channel_ids)

    def fetch(self, *, since_id: str | None, limit: int) -> Sequence[RawPost]:
        raise HistoryUnavailable(
            "read-only MTProto history requires an approved session with real credentials "
            "(integration blocked until provided); no local fallback is invented"
        )


class TelegramClient:
    """Read-only Telegram client (Bot API snapshot + history source)."""

    source = TELEGRAM_SOURCE

    def __init__(
        self,
        transport: Transport,
        *,
        bot_token: str | None = None,
        history: PostSource | None = None,
        allowed_channel_ids: Iterable[str] = (),
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._transport = transport
        self._bot_token = bot_token
        self._history = history
        self._allowed_channels = frozenset(allowed_channel_ids)
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        return (
            f"TelegramClient(base_url={self._base_url!r}, bot_token={'set' if self._bot_token else None}, "
            f"history={'set' if self._history else None}, channels={len(self._allowed_channels)})"
        )

    # --- subscriber snapshot ---------------------------------------------- #

    def snapshot_subscribers(self, *, chat_id: str, on_date: date, now: datetime | None = None) -> ChannelSubscriberPoint:
        """Read the member count once a day. A failure raises; it is not zero."""

        if self._bot_token is None:
            raise TelegramError("no bot token configured")
        response = self._get(f"/bot{self._bot_token}/getChatMemberCount", {"chat_id": chat_id})
        if not response.ok:
            raise TelegramError(f"getChatMemberCount failed with HTTP {response.status}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramProtocolError("getChatMemberCount returned non-JSON") from exc
        if not payload.get("ok"):
            raise TelegramError(f"getChatMemberCount error: {payload.get('description', 'unknown')}")
        result = payload.get("result")
        if isinstance(result, bool) or not isinstance(result, int) or result < 0:
            raise TelegramProtocolError(f"unexpected member count {result!r}")
        return ChannelSubscriberPoint(
            on=on_date,
            subscribers=result,
            received_at=(now or datetime.now(timezone.utc)),
        )

    # --- post history ------------------------------------------------------ #

    def pull_posts(self, *, channel_id: str, since_id: str | None = None, limit: int = 100) -> tuple[RawPost, ...]:
        if self._allowed_channels and channel_id not in self._allowed_channels:
            raise TelegramError(f"refusing to read a channel outside the allowlist: {channel_id!r}")
        if not self._allowed_channels:
            raise TelegramError("no channel allowlist configured; refusing to read any channel")
        if self._history is None:
            raise HistoryUnavailable("no read-only history source configured")
        posts = tuple(self._history.fetch(since_id=since_id, limit=limit))
        for post in posts:
            if post.channel_id != channel_id:
                raise TelegramError("history source returned a post from another channel")
        return posts

    def _get(self, path: str, params: dict[str, str]) -> Response:
        request = Request(
            method="GET",
            url=f"{self._base_url}{path}",
            params=params,
            headers={"Accept": "application/json"},
            timeout_s=self._timeout_s,
        )
        try:
            return self._transport.send(request)
        except HttpStatusError:
            raise
        except TransportError as exc:
            raise TelegramError(str(exc)) from exc


def bot_api_history_limitation() -> str:
    """Documented reason the Bot API is not used for history."""

    return (
        "Bot API getUpdates keeps updates for at most 24 hours; a daily poll cannot guarantee "
        "history and loses data on a single failure. History comes from a read-only source instead."
    )
