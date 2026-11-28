"""The «Что пересылали» ranking: top-3 event announcements by measured forwards.

Rules (the task's §6, frozen here):

* window — the last 30 calendar days by Moscow time, *including* the current
  day; a post belongs by its publication day, never by the event date;
* only announcement-classified posts take part — the classification is the one
  the editorial pipeline already stored, nothing is re-classified here;
* a media group is one publication: the counters of its first (main) message
  stand for the whole album, the album's other messages never duplicate it;
* sort by forwards descending, then fresher first, then message id — views,
  reactions and any «engagement» never influence the order;
* at most three cards, fewer when fewer qualify; a post with an unknown
  forwards count is excluded, not zeroed.

The function reads only the private store; the counters arrive from the nightly
MTProto measurement (``randreport.connectors.telethon_stats``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from ..domain.dto import AnnouncementsDTO, ForwardItemDTO
from ..domain.timeframes import msk_day
from ..store import repository as repo

#: The public channel the t.me links point to (same constant the pipeline uses).
CHANNEL_USERNAME = "random_culture"

#: Sliding window length in calendar days (the task fixes 30).
WINDOW_DAYS = 30

#: Maximum cards in the ranking (the task fixes 3).
MAX_CARDS = 3

#: Longest fallback title cut from a post text when the news desk stored none.
TITLE_LIMIT = 120


@dataclass(frozen=True)
class Candidate:
    """One announcement publication of the window, media group already collapsed."""

    message_id: str
    title: str
    posted_at: datetime
    forwards: int | None  # None = not measured yet → excluded from the ranking


def ranking_window(day: date, *, days: int = WINDOW_DAYS) -> tuple[date, date]:
    """The inclusive ``(from, to)`` window: 30 Moscow calendar days ending today."""

    return day - timedelta(days=days - 1), day


def _post_title(post_text: str, stored_title: str | None) -> str:
    """The desk's title, or the first meaningful line of the post itself."""

    if stored_title and stored_title.strip():
        return stored_title.strip()
    for line in post_text.splitlines():
        line = line.strip()
        if line:
            return line[:TITLE_LIMIT]
    return "Анонс мероприятия"


def _candidates(
    conn: sqlite3.Connection,
    *,
    window_from: date,
    window_to: date,
) -> tuple[Candidate, ...]:
    """Announcement publications of the window, media groups already collapsed."""

    posts: dict[str, Any] = {}
    album_members: dict[str, list[str]] = {}
    for post in repo.archived_posts(conn):
        if post.deleted:
            continue
        day = msk_day(post.posted_at)
        if not (window_from <= day <= window_to):
            continue
        posts[post.message_id] = post
        key = post.media_album_id or f"id:{post.message_id}"
        album_members.setdefault(key, []).append(post.message_id)

    counters = {row["message_id"]: row for row in repo.post_counter_rows(conn)}
    mains: dict[str, Candidate] = {}
    for item in repo.announcement_news_items(conn):
        try:
            cited = [str(mid) for mid in json.loads(item["source_message_ids"] or "[]")]
        except (TypeError, ValueError):
            cited = []
        members: list[str] = []
        for mid in cited:
            archived = posts.get(mid)
            if archived is None:
                continue
            key = archived.media_album_id or f"id:{mid}"
            members.extend(m for m in album_members.get(key, ()) if m not in members)
        if not members:
            continue
        # the main message of a media group is its first post (lowest id); its
        # counter stands for the whole album, others never duplicate it
        main_id = min(members, key=int)
        if main_id in mains:
            continue
        post = posts[main_id]
        row = counters.get(main_id)
        forwards = None if row is None or row["forwards"] is None else int(row["forwards"])
        mains[main_id] = Candidate(
            message_id=main_id,
            title=_post_title(post.text, item.get("title")),
            posted_at=post.posted_at,
            forwards=forwards,
        )
    return tuple(mains.values())


def ranked(items: Sequence[Candidate], *, limit: int = MAX_CARDS) -> tuple[Candidate, ...]:
    """The deterministic order: forwards ↓, fresher ↑, id ↑; unknowns excluded."""

    known = [item for item in items if item.forwards is not None]
    ordered = sorted(known, key=lambda item: (-int(item.forwards or 0), -item.posted_at.timestamp(), int(item.message_id)))
    return tuple(ordered[: max(limit, 0)])


def build_announcements_section(
    conn: sqlite3.Connection,
    *,
    cutoff: datetime,
    freshness: str,
    verification: str,
    completeness: str,
    source_labels: Sequence[str],
    channel_username: str = CHANNEL_USERNAME,
) -> AnnouncementsDTO:
    """The public DTO of the ranking for one build day (release-id agnostic)."""

    window_from, window_to = ranking_window(msk_day(cutoff))
    candidates = _candidates(conn, window_from=window_from, window_to=window_to)
    top = ranked(candidates)
    items = tuple(
        ForwardItemDTO(
            rank=index,
            title=item.title,
            url=f"https://t.me/{channel_username}/{item.message_id}",
            forwards=int(item.forwards or 0),
            published_on=msk_day(item.posted_at),
        )
        for index, item in enumerate(top, start=1)
    )
    return AnnouncementsDTO(
        as_of=msk_day(cutoff),
        release_id="",  # stamped by the builder together with the other sections
        freshness=freshness,
        verification=verification,
        completeness=completeness,
        source_labels=tuple(source_labels),
        window_from=window_from,
        window_to=window_to,
        fetched_at=repo.latest_counter_fetch(conn),
        items=items,
    )
