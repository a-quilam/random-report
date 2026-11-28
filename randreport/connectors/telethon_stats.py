"""Forwarding counters over MTProto, with a bot-token login (Telethon 1.x).

The HTTP Bot API does not expose forward counters; a Telethon client logged in
as an ordinary bot does (``get_messages`` → ``forwards``/``views``), and the
owner's experiment on 2026-11-28 proved the method on real posts.  This module
is the only place that touches Telethon:

* credentials come from the process environment only (``TELEGRAM_API_ID``,
  ``TELEGRAM_API_HASH``, ``TELEGRAM_STATS_BOT_TOKEN``, ``TELEGRAM_CHANNEL_USERNAME``);
  nothing here logs, stores or returns them;
* the message ids are supplied by the caller — the nightly adapter passes the
  archived posts of the rolling 30-day window — because a bot must not rely on
  ``getHistory``;
* Telethon is imported lazily inside :meth:`TelethonForwardReader.fetch`, so the
  rest of the project (and every test) runs without the dependency installed;
* errors are contained: a deleted message is a skip, a batch failure is
  recorded, a ``FloodWaitError`` stops the pass instead of retrying in a loop —
  the next nightly pass simply re-measures the window again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Sequence

#: The public channel whose posts are measured; overridable via the environment.
DEFAULT_CHANNEL_USERNAME = "random_culture"

#: get_messages accepts a list of ids; 100 per call is a reasonable batch.
BATCH_SIZE = 100


class StatsConfigError(RuntimeError):
    """The environment does not name everything the MTProto reader needs."""


class StatsUnavailable(RuntimeError):
    """Telethon is not installed, or Telegram could not be reached / authorised."""


@dataclass(frozen=True)
class ForwardStatsConfig:
    """Everything the reader needs; values are secrets and never printed."""

    api_id: int
    api_hash: str
    bot_token: str
    channel_username: str

    def __repr__(self) -> str:  # pragma: no cover - defence against accidental leaks
        return "ForwardStatsConfig(<secrets hidden>, channel=…)"


def resolve_config(env: Mapping[str, str] | None = None) -> ForwardStatsConfig:
    """Read the MTProto credentials from the environment; raise when incomplete.

    ``TELEGRAM_CHANNEL_USERNAME`` is optional and defaults to the public channel;
    the nightly notes a missing variable instead of failing the source, so a
    machine without the secrets keeps syncing everything else as before.
    """

    source = os.environ if env is None else env
    api_id_raw = (source.get("TELEGRAM_API_ID") or "").strip()
    api_hash = (source.get("TELEGRAM_API_HASH") or "").strip()
    bot_token = (source.get("TELEGRAM_STATS_BOT_TOKEN") or "").strip()
    channel = (source.get("TELEGRAM_CHANNEL_USERNAME") or source.get("TELEGRAM_PUBLIC_CHANNEL") or "").strip()
    missing = [
        name
        for name, value in (
            ("TELEGRAM_API_ID", api_id_raw),
            ("TELEGRAM_API_HASH", api_hash),
            ("TELEGRAM_STATS_BOT_TOKEN", bot_token),
        )
        if not value
    ]
    if missing:
        raise StatsConfigError(f"missing environment variables: {', '.join(missing)}")
    if not api_id_raw.isdigit():
        raise StatsConfigError("TELEGRAM_API_ID must be a numeric api_id")
    return ForwardStatsConfig(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        bot_token=bot_token,
        channel_username=(channel or DEFAULT_CHANNEL_USERNAME).lstrip("@"),
    )


@dataclass(frozen=True)
class PostCounters:
    """One measured post: ``None`` means Telegram showed no counter at all."""

    message_id: str
    channel_id: str
    forwards: int | None
    views: int | None


@dataclass(frozen=True)
class FetchReport:
    """What one pass managed to read before it stopped."""

    counters: tuple[PostCounters, ...]
    fetched_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]  # deleted posts, service messages, no counter object
    failed_batches: tuple[str, ...]
    flood_wait_s: int | None = None

    @property
    def complete(self) -> bool:
        return not self.failed_batches and self.flood_wait_s is None


class TelethonForwardReader:
    """Batch-read ``forwards``/``views`` for explicit message ids."""

    def __init__(self, config: ForwardStatsConfig, *, batch_size: int = BATCH_SIZE) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._config = config
        self._batch_size = batch_size

    @property
    def channel_id(self) -> str:
        return self._config.channel_username

    def fetch(self, message_ids: Sequence[str]) -> FetchReport:
        """Measure the given ids; never raises for a Telegram-side problem.

        A connection/auth failure raises :class:`StatsUnavailable`; everything
        Telegram answers for is returned, a FloodWait stops the pass with the
        server's wait seconds reported instead of sleeping through it.
        """

        ids = [str(mid) for mid in message_ids if str(mid).isdigit()]
        if not ids:
            return FetchReport((), (), (), ())
        try:
            from telethon.sync import TelegramClient
            from telethon.errors import FloodWaitError
            from telethon.sessions import MemorySession
        except ImportError as exc:  # pragma: no cover - depends on the deployment
            raise StatsUnavailable(
                "telethon is not installed in this environment; the forward counters cannot be measured"
            ) from exc

        counters: list[PostCounters] = []
        fetched: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        flood_wait: int | None = None
        client = TelegramClient(MemorySession(), self._config.api_id, self._config.api_hash)
        try:
            client.start(bot_token=self._config.bot_token)
            for start in range(0, len(ids), self._batch_size):
                batch = ids[start : start + self._batch_size]
                try:
                    posts = client.get_messages(
                        f"@{self._config.channel_username}",
                        ids=[int(mid) for mid in batch],
                    )
                except FloodWaitError as exc:
                    # respect the limit: stop now, report the wait, no retry loop
                    flood_wait = int(exc.seconds)
                    break
                except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the pass
                    failed.extend(batch)
                    continue
                seen: set[int] = set()
                for post in posts or ():
                    if post is None:
                        continue
                    seen.add(int(post.id))
                    forwards = getattr(post, "forwards", None)
                    views = getattr(post, "views", None)
                    counters.append(
                        PostCounters(
                            message_id=str(post.id),
                            channel_id=self._config.channel_username,
                            forwards=None if forwards is None else int(forwards),
                            views=None if views is None else int(views),
                        )
                    )
                for mid in batch:
                    numeric = int(mid)
                    if numeric in seen:
                        fetched.append(mid)
                    else:
                        skipped.append(mid)  # deleted or never existed
        except StatsUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - auth/connection problems surface as one error
            raise StatsUnavailable(f"telegram mtproto read failed: {type(exc).__name__}") from exc
        finally:
            try:
                client.disconnect()
            except Exception:  # pragma: no cover - disconnect is best-effort
                pass
        return FetchReport(
            counters=tuple(counters),
            fetched_ids=tuple(fetched),
            skipped_ids=tuple(skipped),
            failed_batches=tuple(failed),
            flood_wait_s=flood_wait,
        )
