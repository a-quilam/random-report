"""Import a missed public post export into the archive (section 8 backfill).

Section 8 requires a reliable way to recover posts the intake did not see:
"at a failure or an incomplete history show news stale/gap, [and] the ability to
import public posts".  This module is that path, and it is deliberately narrow:

* it reads a **public** export (the JSON Telegram Desktop produces for a public
  channel, or a plain list of posts) and writes it into
  ``public_message_archive`` — the same private archive the webhook inbox fills;
* it never touches money, never writes a public payload, invents no post and no
  date, and never contacts Telegram;
* an entry it cannot read is *counted and reported*, never guessed: a bad date or
  a missing id is a rejected row, not a fabricated post;
* the import is recorded (``news_imports`` plus an audit entry), so a reviewer
  can see that a period came partly from an import rather than from intake.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..connectors.telegram import RawPost
from ..store import repository as repo
from ..store.db import transaction

IMPORT_SOURCE = "public_export"
MAX_IMPORT_POSTS = 5000
MAX_IMPORT_TEXT = 8192


class ImportRejected(ValueError):
    """The payload itself is not a public post export."""


@dataclass(frozen=True)
class ImportReport:
    import_id: str
    channel_id: str
    source_label: str
    received: int
    imported: int
    unchanged: int
    rejected: int
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "channel_id": self.channel_id,
            "source_label": self.source_label,
            "received": self.received,
            "imported": self.imported,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "reasons": list(self.reasons),
        }


def _entries(payload: Any) -> Sequence[Any]:
    if isinstance(payload, Mapping):
        for key in ("messages", "posts", "items"):
            if key in payload:
                value = payload[key]
                if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                    raise ImportRejected(f"{key} must be a list")
                return list(value)
        raise ImportRejected("the export has no messages/posts list")
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return list(payload)
    raise ImportRejected("the export must be a JSON object or a list")


def _text_of(value: Any) -> str | None:
    """Telegram Desktop rich text is a list of strings and entities; flatten it."""

    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return None


def _date_of(entry: Mapping[str, Any]) -> datetime | None:
    for key in ("date", "posted_at", "date_iso"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    unixtime = entry.get("date_unixtime")
    if isinstance(unixtime, str) and unixtime.strip().isdigit():
        unixtime = int(unixtime)
    if isinstance(unixtime, int) and not isinstance(unixtime, bool) and unixtime > 0:
        return datetime.fromtimestamp(unixtime, tz=timezone.utc)
    return None


def _edited_of(entry: Mapping[str, Any]) -> datetime | None:
    for key in ("edited_at", "edit_date_iso"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    edit_date = entry.get("edit_date")
    if isinstance(edit_date, int) and not isinstance(edit_date, bool) and edit_date > 0:
        return datetime.fromtimestamp(edit_date, tz=timezone.utc)
    return None


def parse_public_export(payload: Any, *, channel_id: str) -> tuple[tuple[RawPost, ...], tuple[str, ...]]:
    """Read a public export; return the readable posts and why the rest was rejected."""

    entries = _entries(payload)
    if len(entries) > MAX_IMPORT_POSTS:
        raise ImportRejected(f"the export has more than {MAX_IMPORT_POSTS} entries")
    posts: list[RawPost] = []
    reasons: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            reasons.append(f"entry {index}: not an object")
            continue
        raw_id = entry.get("id", entry.get("message_id"))
        if isinstance(raw_id, bool) or raw_id is None:
            reasons.append(f"entry {index}: no message id")
            continue
        message_id = str(raw_id).strip()
        if not message_id.isdigit():
            reasons.append(f"entry {index}: message id {message_id!r} is not numeric")
            continue
        text = _text_of(entry.get("text", entry.get("caption")))
        if text is None or not text.strip():
            reasons.append(f"entry {index}: no text")
            continue
        if len(text) > MAX_IMPORT_TEXT:
            reasons.append(f"entry {index}: text is longer than {MAX_IMPORT_TEXT} characters")
            continue
        posted_at = _date_of(entry)
        if posted_at is None:
            reasons.append(f"entry {index}: no readable date")
            continue
        if message_id in seen:
            reasons.append(f"entry {index}: duplicate message id {message_id}")
            continue
        seen.add(message_id)
        posts.append(
            RawPost(
                message_id=message_id,
                channel_id=channel_id,
                posted_at=posted_at,
                text=text,
                edited_at=_edited_of(entry),
            )
        )
    return tuple(posts), tuple(reasons)


def _import_id(channel_id: str, source_label: str, posts: Sequence[RawPost]) -> str:
    digest = hashlib.sha256(
        "\x1f".join([channel_id, source_label, *(f"{post.message_id}:{post.text}" for post in posts)]).encode("utf-8")
    ).hexdigest()[:16]
    return f"imp_{digest}"


def import_public_posts(
    conn: sqlite3.Connection,
    payload: Any,
    *,
    channel_id: str,
    source_label: str = IMPORT_SOURCE,
    now: datetime | None = None,
) -> ImportReport:
    """Backfill a public export into the archive, without touching any money fact."""

    if not channel_id:
        raise ImportRejected("an explicit channel id is required for an import")
    posts, reasons = parse_public_export(payload, channel_id=channel_id)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    counts = repo.upsert_posts(conn, posts, now=stamp) if posts else repo.UpsertCounts()
    report = ImportReport(
        import_id=_import_id(channel_id, source_label, posts),
        channel_id=channel_id,
        source_label=source_label,
        received=len(posts) + len(set(reasons)),
        imported=counts.new,
        unchanged=counts.changed + counts.unchanged,
        rejected=len(set(reasons)),
        reasons=tuple(sorted(set(reasons))),
    )
    with transaction(conn):
        conn.execute(
            """
            INSERT OR REPLACE INTO news_imports (
                import_id, channel_id, source_label, received, imported, unchanged, rejected, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.import_id,
                report.channel_id,
                report.source_label,
                report.received,
                report.imported,
                report.unchanged,
                report.rejected,
                json.dumps(list(report.reasons), ensure_ascii=False),
                stamp,
            ),
        )
    repo.audit(
        conn,
        action="news_import",
        entity_type="channel",
        entity_key=channel_id,
        detail={"import_id": report.import_id, "imported": report.imported, "rejected": report.rejected},
        now=stamp,
    )
    return report
