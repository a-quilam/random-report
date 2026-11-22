"""Moderating the machine-extracted chronicle (sections 3.6, 4, 8; N02, A02).

The chronicle publishes machine-extracted news. A human can **edit**, **merge**
or **hide** a result, and the desk keeps all three layers of the item:

* the LLM version (revision 1, as the extractor produced it),
* the manual correction (a new revision, ``origin='manual'``),
* the audit entry naming who changed what and why.

A later nightly run never undoes a manual decision (the store keeps
``manual_state`` items), and a merge folds the source messages of the absorbed
item into the surviving one so a re-extraction of the same messages is hidden
instead of re-published. No user text is ever rendered unescaped — the renderer
escapes, and this module stores plain text only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..store import repository as repo
from ..store.db import transaction
from . import checks

ACTIVE = "active"
HIDDEN = "hidden"
EDITED = "edited"
MERGED = "merged"

MAX_TITLE = 200
MAX_SUMMARY = 2000
MAX_COMMENT = 1000


class NewsError(ValueError):
    """A moderation action that must not be applied."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Revision:
    news_id: str
    revision: int
    origin: str
    classification: str
    category: str | None
    title: str | None
    summary: str | None
    source_message_ids: tuple[str, ...]
    actor: str | None
    note: str | None
    created_at: str


def _clean(value: Any, *, limit: int, what: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise NewsError(f"{what} не может быть пустым")
    if len(text) > limit:
        raise NewsError(f"{what} длиннее {limit} символов")
    return text


def revisions(conn: sqlite3.Connection, news_id: str) -> tuple[Revision, ...]:
    rows = conn.execute(
        "SELECT * FROM news_revisions WHERE news_id = ? ORDER BY revision", (news_id,)
    ).fetchall()
    return tuple(
        Revision(
            news_id=str(row["news_id"]),
            revision=int(row["revision"]),
            origin=str(row["origin"]),
            classification=str(row["classification"]),
            category=row["category"],
            title=row["title"],
            summary=row["summary"],
            source_message_ids=tuple(json.loads(row["source_message_ids"])),
            actor=row["actor"],
            note=row["note"],
            created_at=str(row["created_at"]),
        )
        for row in rows
    )


def _record_revision(
    conn: sqlite3.Connection,
    *,
    news_id: str,
    origin: str,
    classification: str,
    category: str | None,
    title: str | None,
    summary: str | None,
    source_message_ids: Sequence[str],
    actor: str | None,
    note: str | None,
    stamp: str,
) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) AS latest FROM news_revisions WHERE news_id = ?", (news_id,)
    ).fetchone()
    revision = int(row["latest"]) + 1
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO news_revisions (news_id, revision, origin, classification, category, title, summary, source_message_ids, actor, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                news_id,
                revision,
                origin,
                classification,
                category,
                title,
                summary,
                json.dumps(sorted(set(source_message_ids))),
                actor,
                note,
                stamp,
            ),
        )
    return revision


def ensure_history(conn: sqlite3.Connection, news_id: str, *, now: str | None = None) -> int:
    """Make sure revision 1 exists for an item produced by the extractor."""

    existing = revisions(conn, news_id)
    if existing:
        return existing[0].revision
    row = conn.execute("SELECT * FROM news_items WHERE news_id = ?", (news_id,)).fetchone()
    if row is None:
        raise NewsError("нет такой новости")
    return _record_revision(
        conn,
        news_id=news_id,
        origin=str(row["origin"]),
        classification=str(row["classification"]),
        category=row["category"],
        title=row["title"],
        summary=row["summary"],
        source_message_ids=json.loads(row["source_message_ids"]),
        actor=None,
        note="версия источника",
        stamp=now or _utcnow(),
    )


def desk_items(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Every stored item with its moderation state and its source references."""

    from ..ops.pipeline import NEWS_CHANNEL_USERNAME

    rows = conn.execute("SELECT * FROM news_items ORDER BY published_on, news_id").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        ids = sorted(json.loads(row["source_message_ids"]))
        item["source_message_ids"] = ids
        item["source_urls"] = [f"https://t.me/{NEWS_CHANNEL_USERNAME}/{mid}" for mid in ids]
        item["revisions"] = list(revisions(conn, str(row["news_id"])))
        out.append(item)
    return tuple(out)


def public_items(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Exactly what the public chronicle publishes, by the store's own rule."""

    return repo.public_news_items(conn)


def edit(
    conn: sqlite3.Connection,
    *,
    news_id: str,
    actor: str,
    title: str | None = None,
    summary: str | None = None,
    category: str | None = None,
    note: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply a manual correction as a new revision; the LLM version stays."""

    row = conn.execute("SELECT * FROM news_items WHERE news_id = ?", (news_id,)).fetchone()
    if row is None:
        raise NewsError("нет такой новости")
    ensure_history(conn, news_id, now=now)
    new_title = _clean(title, limit=MAX_TITLE, what="заголовок") if title is not None else row["title"]
    new_summary = _clean(summary, limit=MAX_SUMMARY, what="текст") if summary is not None else row["summary"]
    new_category = row["category"] if category is None else ("" if category.strip() == "" else category.strip())
    if title is None and summary is None and category is None:
        raise NewsError("нечего исправлять")
    stamp = now or _utcnow()
    revision = _record_revision(
        conn,
        news_id=news_id,
        origin="manual",
        classification=str(row["classification"]),
        category=new_category,
        title=new_title,
        summary=new_summary,
        source_message_ids=json.loads(row["source_message_ids"]),
        actor=actor,
        note=note or "ручное исправление",
        stamp=stamp,
    )
    with transaction(conn):
        conn.execute(
            """
            UPDATE news_items
               SET title = ?, summary = ?, category = ?, manual_state = ?, origin = 'manual',
                   machine_processed = 1, updated_at = ?
             WHERE news_id = ?
            """,
            (new_title, new_summary, new_category, EDITED, stamp, news_id),
        )
    repo.audit(
        conn,
        action="news.edited",
        actor=actor,
        entity_type="news_item",
        entity_key=news_id,
        detail={"revision": revision, "note": note},
        now=stamp,
    )
    checks.invalidate_for_news(conn, news_id=news_id, actor=actor, now=stamp)
    return {"news_id": news_id, "revision": revision, "manual_state": EDITED}


def hide(conn: sqlite3.Connection, *, news_id: str, actor: str, reason: str, now: str | None = None) -> dict[str, Any]:
    if not reason.strip():
        raise NewsError("скрытие требует причины")
    row = conn.execute("SELECT manual_state FROM news_items WHERE news_id = ?", (news_id,)).fetchone()
    if row is None:
        raise NewsError("нет такой новости")
    stamp = now or _utcnow()
    repo.set_manual_state(conn, news_id=news_id, state=HIDDEN, now=stamp)
    repo.audit(conn, action="news.hidden", actor=actor, entity_type="news_item", entity_key=news_id, detail={"reason": reason}, now=stamp)
    checks.invalidate_for_news(conn, news_id=news_id, actor=actor, now=stamp)
    return {"news_id": news_id, "manual_state": HIDDEN}


def restore(conn: sqlite3.Connection, *, news_id: str, actor: str, version: int | None = None, now: str | None = None) -> dict[str, Any]:
    """Un-hide an item, optionally restoring a specific stored revision."""

    row = conn.execute("SELECT * FROM news_items WHERE news_id = ?", (news_id,)).fetchone()
    if row is None:
        raise NewsError("нет такой новости")
    ensure_history(conn, news_id, now=now)
    stamp = now or _utcnow()
    if version is not None:
        wanted = [item for item in revisions(conn, news_id) if item.revision == int(version)]
        if not wanted:
            raise NewsError("нет такой версии")
        chosen = wanted[0]
        state = EDITED if chosen.origin == "manual" else ACTIVE
        with transaction(conn):
            conn.execute(
                """
                UPDATE news_items SET title = ?, summary = ?, category = ?, manual_state = ?, updated_at = ?
                 WHERE news_id = ?
                """,
                (chosen.title, chosen.summary, chosen.category, state, stamp, news_id),
            )
    else:
        state = EDITED if str(row["manual_state"]) == HIDDEN and str(row["origin"]) == "manual" else ACTIVE
        repo.set_manual_state(conn, news_id=news_id, state=state, now=stamp)
    repo.audit(
        conn,
        action="news.restored",
        actor=actor,
        entity_type="news_item",
        entity_key=news_id,
        detail={"version": version, "state": state},
        now=stamp,
    )
    checks.invalidate_for_news(conn, news_id=news_id, actor=actor, now=stamp)
    return {"news_id": news_id, "manual_state": state, "version": version}


def merge(conn: sqlite3.Connection, *, target_id: str, source_id: str, actor: str, reason: str, now: str | None = None) -> dict[str, Any]:
    """Fold one item into another; the absorbed item is hidden and remembered."""

    if source_id == target_id:
        raise NewsError("нельзя объединить новость с самой собой")
    if not reason.strip():
        raise NewsError("объединение требует причины")
    target = conn.execute("SELECT * FROM news_items WHERE news_id = ?", (target_id,)).fetchone()
    source = conn.execute("SELECT * FROM news_items WHERE news_id = ?", (source_id,)).fetchone()
    if target is None or source is None:
        raise NewsError("нет такой новости")
    ensure_history(conn, target_id, now=now)
    ensure_history(conn, source_id, now=now)
    ids = sorted(set(json.loads(target["source_message_ids"])) | set(json.loads(source["source_message_ids"])))
    stamp = now or _utcnow()
    _record_revision(
        conn,
        news_id=target_id,
        origin="manual",
        classification=str(target["classification"]),
        category=target["category"],
        title=target["title"],
        summary=target["summary"],
        source_message_ids=ids,
        actor=actor,
        note=f"объединение с {source_id}: {reason}",
        stamp=stamp,
    )
    with transaction(conn):
        conn.execute(
            "UPDATE news_items SET source_message_ids = ?, manual_state = ?, updated_at = ? WHERE news_id = ?",
            (json.dumps(ids), EDITED if target["manual_state"] == ACTIVE else target["manual_state"], stamp, target_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO news_merges (merge_id, target_news_id, source_news_id, actor, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"mrg_{target_id}_{source_id}", target_id, source_id, actor, reason, stamp),
        )
    repo.set_manual_state(conn, news_id=source_id, state=HIDDEN, now=stamp)
    repo.audit(
        conn,
        action="news.merged",
        actor=actor,
        entity_type="news_item",
        entity_key=target_id,
        detail={"absorbed": source_id, "reason": reason, "source_ids": ids},
        now=stamp,
    )
    checks.invalidate_for_news(conn, news_id=source_id, actor=actor, now=stamp)
    checks.invalidate_for_news(conn, news_id=target_id, actor=actor, now=stamp)
    return {"target": target_id, "absorbed": source_id, "source_message_ids": ids}


def absorbed_message_ids(conn: sqlite3.Connection) -> dict[str, str]:
    """message id → the surviving news id, for every message a merge absorbed."""

    mapping: dict[str, str] = {}
    for row in conn.execute("SELECT * FROM news_merges").fetchall():
        target = conn.execute(
            "SELECT source_message_ids FROM news_items WHERE news_id = ?", (str(row["target_news_id"]),)
        ).fetchone()
        if target is None:
            continue
        for message_id in json.loads(target["source_message_ids"]):
            mapping[str(message_id)] = str(row["target_news_id"])
    return mapping


def reconcile_merges(conn: sqlite3.Connection, *, now: str | None = None) -> int:
    """Hide machine duplicates whose messages a human already merged away.

    A later run can produce a *new* item id from the same source messages (the id
    is content-addressed on the messages plus the prompt version). Such a
    re-created duplicate must not reach the public chronicle, so it is hidden
    with an audit entry instead of being published twice.
    """

    absorbed = absorbed_message_ids(conn)
    if not absorbed:
        return 0
    stamp = now or _utcnow()
    hidden = 0
    rows = conn.execute("SELECT * FROM news_items WHERE manual_state = 'active'").fetchall()
    for row in rows:
        ids = set(json.loads(row["source_message_ids"]))
        if not ids or not ids <= set(absorbed):
            continue
        repo.set_manual_state(conn, news_id=str(row["news_id"]), state=HIDDEN, now=stamp)
        repo.audit(
            conn,
            action="news.deduplicated",
            actor="system",
            entity_type="news_item",
            entity_key=str(row["news_id"]),
            detail={"absorbed_by": sorted({absorbed[mid] for mid in ids})},
            now=stamp,
        )
        hidden += 1
    return hidden
