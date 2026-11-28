"""Typed repository functions over the private SQLite facts.

These functions are deliberately dumb: they record exactly what a read-only
connector returned and what the builder computed. They never derive a balance,
never invent a movement and never widen a public payload.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..domain.events import Occurrence
from ..domain.finance import AccountBalance, Movement
from ..domain.timeframes import MSK
from .db import transaction

SEPARATOR = "\x1f"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(*parts: Any) -> str:
    payload = SEPARATOR.join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class UpsertCounts:
    new: int = 0
    changed: int = 0
    deleted: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"new": self.new, "changed": self.changed, "deleted": self.deleted, "unchanged": self.unchanged}


# --------------------------------------------------------------------------- #
# Money facts
# --------------------------------------------------------------------------- #

def movement_content_hash(movement: Movement) -> str:
    return _hash(
        movement.id,
        movement.occurred_on.isoformat(),
        movement.amount_kopecks,
        movement.source_account,
        movement.target_account,
        movement.category_id,
        movement.currency,
        movement.pair_id,
        movement.pair_kind,
        movement.reimbursement_of,
        int(movement.is_planned),
        int(movement.deleted),
    )


def upsert_movements(
    conn: sqlite3.Connection,
    movements: Iterable[Movement],
    *,
    now: str | None = None,
) -> UpsertCounts:
    """Insert or update movements; identical re-delivery changes nothing."""

    stamp = now or _utcnow()
    new = changed = deleted = unchanged = 0
    for movement in movements:
        digest = movement_content_hash(movement)
        row = conn.execute(
            "SELECT content_hash, deleted FROM finance_transactions WHERE transaction_id = ?",
            (movement.id,),
        ).fetchone()
        if row is not None and row["content_hash"] == digest:
            unchanged += 1
            continue
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO finance_transactions (
                    transaction_id, occurred_on, amount_kopecks, source_account, target_account,
                    category_id, currency, pair_id, pair_kind, reimbursement_of, is_planned,
                    deleted, source_revision, notes, content_hash, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (transaction_id) DO UPDATE SET
                    occurred_on = excluded.occurred_on,
                    amount_kopecks = excluded.amount_kopecks,
                    source_account = excluded.source_account,
                    target_account = excluded.target_account,
                    category_id = excluded.category_id,
                    currency = excluded.currency,
                    pair_id = excluded.pair_id,
                    pair_kind = excluded.pair_kind,
                    reimbursement_of = excluded.reimbursement_of,
                    is_planned = excluded.is_planned,
                    deleted = excluded.deleted,
                    source_revision = excluded.source_revision,
                    notes = excluded.notes,
                    content_hash = excluded.content_hash,
                    ingested_at = excluded.ingested_at
                """,
                (
                    movement.id,
                    movement.occurred_on.isoformat(),
                    movement.amount_kopecks,
                    movement.source_account,
                    movement.target_account,
                    movement.category_id,
                    movement.currency,
                    movement.pair_id,
                    movement.pair_kind,
                    movement.reimbursement_of,
                    int(movement.is_planned),
                    int(movement.deleted),
                    movement.source_revision,
                    json.dumps(list(movement.notes)),
                    digest,
                    stamp,
                ),
            )
        if row is None:
            new += 1
        elif movement.deleted and not row["deleted"]:
            deleted += 1
        else:
            changed += 1
    return UpsertCounts(new=new, changed=changed, deleted=deleted, unchanged=unchanged)


def mark_movements_deleted(conn: sqlite3.Connection, ids: Sequence[str], *, now: str | None = None) -> int:
    """Mark source-deleted movements; the row and its revision are kept, not erased."""

    if not ids:
        return 0
    stamp = now or _utcnow()
    placeholders = ",".join("?" for _ in ids)
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE finance_transactions SET deleted = 1, ingested_at = ? WHERE transaction_id IN ({placeholders}) AND deleted = 0",
            (stamp, *ids),
        )
        return cursor.rowcount


def movement_months(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Distinct MSK months that hold live facts — cheaply, without loading them."""

    rows = conn.execute(
        """
        SELECT DISTINCT substr(occurred_on, 1, 7) AS month
          FROM finance_transactions
         WHERE deleted = 0 AND is_planned = 0
         ORDER BY month
        """
    ).fetchall()
    return tuple(str(row["month"]) for row in rows)


def load_movements(conn: sqlite3.Connection) -> tuple[Movement, ...]:
    rows = conn.execute("SELECT * FROM finance_transactions").fetchall()
    out: list[Movement] = []
    for row in rows:
        out.append(
            Movement(
                id=row["transaction_id"],
                occurred_on=date.fromisoformat(row["occurred_on"]),
                amount_kopecks=int(row["amount_kopecks"]),
                source_account=row["source_account"],
                target_account=row["target_account"],
                category_id=row["category_id"],
                currency=row["currency"],
                pair_id=row["pair_id"],
                pair_kind=row["pair_kind"],
                reimbursement_of=row["reimbursement_of"],
                is_planned=bool(row["is_planned"]),
                deleted=bool(row["deleted"]),
                source_revision=int(row["source_revision"]),
                notes=tuple(json.loads(row["notes"])),
            )
        )
    return tuple(out)


def upsert_balance_snapshots(
    conn: sqlite3.Connection,
    balances: Iterable[AccountBalance],
    *,
    source: str,
    now: str | None = None,
) -> int:
    stamp = now or _utcnow()
    written = 0
    for balance in balances:
        if balance.as_of is None:
            continue
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO account_balance_snapshots (
                    account_id, as_of, balance_kopecks, currency, is_liquid, confirmed, scope, source, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (account_id, as_of) DO UPDATE SET
                    balance_kopecks = excluded.balance_kopecks,
                    currency = excluded.currency,
                    is_liquid = excluded.is_liquid,
                    confirmed = excluded.confirmed,
                    scope = excluded.scope,
                    source = excluded.source,
                    ingested_at = excluded.ingested_at
                """,
                (
                    balance.account_id,
                    balance.as_of.isoformat(),
                    balance.balance_kopecks,
                    balance.currency,
                    int(balance.is_liquid),
                    int(balance.confirmed),
                    str(balance.scope),
                    source,
                    stamp,
                ),
            )
        written += 1
    return written


def load_balances(conn: sqlite3.Connection) -> tuple[AccountBalance, ...]:
    from ..domain.flows import AccountScope

    rows = conn.execute("SELECT * FROM account_balance_snapshots").fetchall()
    return tuple(
        AccountBalance(
            account_id=row["account_id"],
            currency=row["currency"],
            balance_kopecks=int(row["balance_kopecks"]),
            as_of=date.fromisoformat(row["as_of"]),
            is_liquid=bool(row["is_liquid"]),
            confirmed=bool(row["confirmed"]),
            scope=AccountScope(row["scope"]),
        )
        for row in rows
    )


def record_category_mapping(conn: sqlite3.Connection, *, version: str, payload: Mapping[str, Any], now: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO category_mapping_versions (version, created_at, payload) VALUES (?, ?, ?)",
            (version, now or _utcnow(), json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )


def record_event_mapping(conn: sqlite3.Connection, *, version: str, payload: Mapping[str, Any], now: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO event_type_mapping (version, created_at, payload) VALUES (?, ?, ?)",
            (version, now or _utcnow(), json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )


def load_category_mapping(conn: sqlite3.Connection) -> tuple[str, dict[str, Any]] | None:
    """Newest stored public-contour mapping, or ``None`` when none was recorded."""

    row = conn.execute("SELECT version, payload FROM category_mapping_versions ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return str(row["version"]), json.loads(row["payload"])


def load_event_mapping(conn: sqlite3.Connection) -> tuple[str, dict[str, Any]] | None:
    row = conn.execute("SELECT version, payload FROM event_type_mapping ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return str(row["version"]), json.loads(row["payload"])


# --------------------------------------------------------------------------- #
# Calendar facts
# --------------------------------------------------------------------------- #

def occurrence_content_hash(occurrence: Occurrence) -> str:
    """The digest of everything the source said about an occurrence.

    The revision token is part of it: a source edit changes the stored row and
    invalidates the admin marks bound to the previous revision, even when the
    normalised fields look identical. The token itself stays opaque - it is
    hashed as delivered, never parsed.
    """

    return _hash(
        occurrence.calendar_id,
        occurrence.occurrence_id,
        occurrence.start.astimezone(timezone.utc).isoformat(),
        None if occurrence.end is None else occurrence.end.astimezone(timezone.utc).isoformat(),
        int(occurrence.all_day),
        ",".join(sorted(occurrence.subcalendar_ids)),
        occurrence.title,
        occurrence.series_id,
        occurrence.revision,
        int(occurrence.deleted),
    )


def _delivered_state_stored(row: Any, occurrence: Occurrence) -> bool:
    """Whether the stored retention flags already match the delivered record.

    Retention is our own bookkeeping, not source content: a row archived because
    it had vanished must be marked present again when the source returns it.
    """

    return bool(row["local_archive"]) == occurrence.local_archive and bool(row["source_present"]) == occurrence.source_present


def upsert_occurrences(
    conn: sqlite3.Connection,
    occurrences: Iterable[Occurrence],
    *,
    now: str | None = None,
) -> UpsertCounts:
    stamp = now or _utcnow()
    new = changed = deleted = unchanged = 0
    for occurrence in occurrences:
        digest = occurrence_content_hash(occurrence)
        row = conn.execute(
            "SELECT content_hash, deleted, source_present, local_archive FROM calendar_occurrences"
            " WHERE calendar_id = ? AND occurrence_id = ?",
            (occurrence.calendar_id, occurrence.occurrence_id),
        ).fetchone()
        if row is not None and row["content_hash"] == digest and _delivered_state_stored(row, occurrence):
            unchanged += 1
            continue
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO calendar_occurrences (
                    calendar_id, occurrence_id, series_id, start_utc, end_utc, all_day,
                    subcalendar_ids, title, raw_type, revision, deleted, source_present,
                    local_archive, content_hash, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (calendar_id, occurrence_id) DO UPDATE SET
                    series_id = excluded.series_id,
                    start_utc = excluded.start_utc,
                    end_utc = excluded.end_utc,
                    all_day = excluded.all_day,
                    subcalendar_ids = excluded.subcalendar_ids,
                    title = excluded.title,
                    raw_type = excluded.raw_type,
                    revision = excluded.revision,
                    deleted = excluded.deleted,
                    source_present = excluded.source_present,
                    local_archive = excluded.local_archive,
                    content_hash = excluded.content_hash,
                    ingested_at = excluded.ingested_at
                """,
                (
                    occurrence.calendar_id,
                    occurrence.occurrence_id,
                    occurrence.series_id,
                    occurrence.start.astimezone(timezone.utc).isoformat(),
                    None if occurrence.end is None else occurrence.end.astimezone(timezone.utc).isoformat(),
                    int(occurrence.all_day),
                    json.dumps(sorted(occurrence.subcalendar_ids)),
                    occurrence.title,
                    occurrence.raw_type,
                    occurrence.revision,
                    int(occurrence.deleted),
                    int(occurrence.source_present),
                    int(occurrence.local_archive),
                    digest,
                    stamp,
                ),
            )
        if row is None:
            new += 1
        elif occurrence.deleted and not row["deleted"]:
            deleted += 1
        else:
            changed += 1
    return UpsertCounts(new=new, changed=changed, deleted=deleted, unchanged=unchanged)


def archive_missing_occurrences(
    conn: sqlite3.Connection,
    *,
    calendar_id: str,
    present_ids: Sequence[str],
    now: str | None = None,
) -> int:
    """Keep locally the occurrences that vanished from the source window.

    Source retention must never erase our own history (docs/REQUIREMENTS.md
    section 7). Missing rows are marked ``local_archive = 1`` and stay counted.
    """

    stamp = now or _utcnow()
    if present_ids:
        placeholders = ",".join("?" for _ in present_ids)
        where = f"AND occurrence_id NOT IN ({placeholders})"
        params: tuple[Any, ...] = (stamp, calendar_id, *present_ids)
    else:
        # an empty window means the source returned nothing: every local row is missing
        where = ""
        params = (stamp, calendar_id)
    with transaction(conn):
        cursor = conn.execute(
            f"""
            UPDATE calendar_occurrences
               SET local_archive = 1, source_present = 0, ingested_at = ?
             WHERE calendar_id = ?
               AND deleted = 0
               AND local_archive = 0
               {where}
            """,
            params,
        )
        return cursor.rowcount


def mark_occurrences_deleted(
    conn: sqlite3.Connection,
    *,
    calendar_id: str,
    ids: Sequence[str],
    now: str | None = None,
) -> int:
    """Mark occurrences the source explicitly deleted.

    The row survives with its content hash marked so a repeated delivery of the
    deletion is idempotent, while a later re-delivery of a live record clears the
    flag again (docs/REQUIREMENTS.md section 7).
    """

    if not ids:
        return 0
    stamp = now or _utcnow()
    placeholders = ",".join("?" for _ in ids)
    with transaction(conn):
        cursor = conn.execute(
            f"""
            UPDATE calendar_occurrences
               SET deleted = 1, content_hash = content_hash || ':deleted', ingested_at = ?
             WHERE calendar_id = ?
               AND occurrence_id IN ({placeholders})
               AND deleted = 0
            """,
            (stamp, calendar_id, *ids),
        )
        return cursor.rowcount


def load_occurrences(conn: sqlite3.Connection) -> tuple[Occurrence, ...]:
    rows = conn.execute("SELECT * FROM calendar_occurrences").fetchall()
    out: list[Occurrence] = []
    for row in rows:
        out.append(
            Occurrence(
                calendar_id=row["calendar_id"],
                occurrence_id=row["occurrence_id"],
                start=datetime.fromisoformat(row["start_utc"]),
                end=None if row["end_utc"] is None else datetime.fromisoformat(row["end_utc"]),
                subcalendar_ids=tuple(json.loads(row["subcalendar_ids"])),
                series_id=row["series_id"],
                title=row["title"],
                all_day=bool(row["all_day"]),
                raw_type=row["raw_type"],
                revision=str(row["revision"]),
                deleted=bool(row["deleted"]),
                source_present=bool(row["source_present"]),
                local_archive=bool(row["local_archive"]),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Channel and posts
# --------------------------------------------------------------------------- #

def record_channel_snapshot(
    conn: sqlite3.Connection,
    *,
    on_date: str,
    subscribers: int,
    source: str,
    received_at: str | datetime | None = None,
    revision: int = 1,
) -> str:
    """Record one daily subscriber snapshot.

    Repeating the same value on the same day does not create a second snapshot
    without a new revision (docs/REQUIREMENTS.md section 8).
    """

    received = received_at.isoformat() if isinstance(received_at, datetime) else (received_at or _utcnow())
    row = conn.execute("SELECT subscribers, revision FROM channel_snapshots WHERE on_date = ?", (on_date,)).fetchone()
    if row is not None:
        if int(row["subscribers"]) == subscribers and int(row["revision"]) >= revision:
            return "unchanged"
        next_revision = max(int(row["revision"]) + 1, revision)
        with transaction(conn):
            conn.execute(
                "UPDATE channel_snapshots SET subscribers = ?, revision = ?, received_at = ?, source = ? WHERE on_date = ?",
                (subscribers, next_revision, received, source, on_date),
            )
        return "revised"
    with transaction(conn):
        conn.execute(
            "INSERT INTO channel_snapshots (on_date, subscribers, source, revision, received_at) VALUES (?, ?, ?, ?, ?)",
            (on_date, subscribers, source, revision, received),
        )
    return "inserted"


def channel_points(conn: sqlite3.Connection) -> tuple[tuple[date, int, str], ...]:
    rows = conn.execute("SELECT on_date, subscribers, source FROM channel_snapshots ORDER BY on_date").fetchall()
    return tuple((date.fromisoformat(row["on_date"]), int(row["subscribers"]), row["source"]) for row in rows)


def upsert_posts(conn: sqlite3.Connection, posts: Iterable[Any], *, now: str | None = None) -> UpsertCounts:
    """Archive channel posts; edits bump the revision, deletion is recorded."""

    stamp = now or _utcnow()
    new = changed = deleted = unchanged = 0
    for post in posts:
        digest = _hash(post.message_id, post.channel_id, post.text, post.posted_at.isoformat(), int(getattr(post, "deleted", False)))
        row = conn.execute(
            "SELECT revision, source_deleted FROM public_message_archive WHERE message_id = ?", (post.message_id,)
        ).fetchone()
        stored_hash = conn.execute(
            "SELECT text, edited_at FROM public_message_archive WHERE message_id = ?", (post.message_id,)
        ).fetchone()
        if row is not None and stored_hash is not None and stored_hash["text"] == post.text and int(row["source_deleted"]) == int(getattr(post, "deleted", False)):
            unchanged += 1
            continue
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO public_message_archive (
                    message_id, channel_id, posted_at, edited_at, text, source_deleted, revision, received_at, media_album_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (message_id) DO UPDATE SET
                    text = excluded.text,
                    edited_at = excluded.edited_at,
                    source_deleted = excluded.source_deleted,
                    revision = excluded.revision,
                    received_at = excluded.received_at,
                    media_album_id = COALESCE(excluded.media_album_id, media_album_id)
                """,
                (
                    post.message_id,
                    post.channel_id,
                    post.posted_at.isoformat(),
                    None if getattr(post, "edited_at", None) is None else post.edited_at.isoformat(),
                    post.text,
                    int(getattr(post, "deleted", False)),
                    int(getattr(post, "revision", 1)),
                    stamp,
                    getattr(post, "media_album_id", None),
                ),
            )
        if row is None:
            new += 1
        elif getattr(post, "deleted", False) and not row["source_deleted"]:
            deleted += 1
        else:
            changed += 1
    return UpsertCounts(new=new, changed=changed, deleted=deleted, unchanged=unchanged)


def archived_posts(conn: sqlite3.Connection, *, include_deleted: bool = False) -> tuple[Any, ...]:
    from ..connectors.telegram import RawPost

    clause = "" if include_deleted else "WHERE source_deleted = 0"
    rows = conn.execute(f"SELECT * FROM public_message_archive {clause} ORDER BY posted_at").fetchall()
    return tuple(
        RawPost(
            message_id=row["message_id"],
            channel_id=row["channel_id"],
            posted_at=datetime.fromisoformat(row["posted_at"]),
            text=row["text"],
            edited_at=None if row["edited_at"] is None else datetime.fromisoformat(row["edited_at"]),
            revision=int(row["revision"]),
            deleted=bool(row["source_deleted"]),
            media_album_id=row["media_album_id"],
        )
        for row in rows
    )


# --------------------------------------------------------------------------- #
# Forwarding counters (MTProto)                                               #
# --------------------------------------------------------------------------- #

def upsert_post_counters(
    conn: sqlite3.Connection,
    counters: Iterable[Any],
    *,
    now: str | None = None,
) -> UpsertCounts:
    """Store measured forwards/views for archived posts.

    One row per message id; a re-measurement overwrites the numbers and the
    ``fetched_at`` stamp.  A post the reader could not answer this pass is simply
    not written, so its previously measured values survive untouched — an unknown
    is never demoted to a zero.
    """

    stamp = now or _utcnow()
    new = changed = unchanged = 0
    for row in counters:
        forwards = None if row.forwards is None else int(row.forwards)
        views = None if row.views is None else int(row.views)
        existing = conn.execute(
            "SELECT forwards, views FROM post_forward_stats WHERE message_id = ?", (row.message_id,)
        ).fetchone()
        if existing is not None and existing["forwards"] == forwards and existing["views"] == views:
            # the numbers repeat: keep the row, refresh the measurement stamp
            with transaction(conn):
                conn.execute(
                    "UPDATE post_forward_stats SET fetched_at = ? WHERE message_id = ?",
                    (stamp, row.message_id),
                )
            unchanged += 1
            continue
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO post_forward_stats (message_id, forwards, views, fetched_at, channel_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (message_id) DO UPDATE SET
                    forwards = excluded.forwards,
                    views = excluded.views,
                    fetched_at = excluded.fetched_at,
                    channel_id = excluded.channel_id
                """,
                (row.message_id, forwards, views, stamp, row.channel_id),
            )
        if existing is None:
            new += 1
        else:
            changed += 1
    return UpsertCounts(new=new, changed=changed, unchanged=unchanged)


def post_counter_rows(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """All stored counter rows, oldest first (``forwards`` NULL = never measured)."""

    rows = conn.execute(
        "SELECT message_id, forwards, views, fetched_at, channel_id FROM post_forward_stats ORDER BY fetched_at"
    ).fetchall()
    return tuple(dict(row) for row in rows)


def latest_counter_fetch(conn: sqlite3.Connection) -> str | None:
    """The newest successful measurement stamp across stored counters, or ``None``."""

    row = conn.execute("SELECT MAX(fetched_at) AS latest FROM post_forward_stats").fetchone()
    return row["latest"] if row is not None else None


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #

def upsert_news_item(conn: sqlite3.Connection, item: Any, *, now: str | None = None) -> str:
    """Insert a machine news item; never overwrite a manual edit or a hidden item.

    Returns ``inserted``, ``unchanged``, ``updated`` or ``preserved``.
    """

    stamp = now or _utcnow()
    row = conn.execute(
        "SELECT manual_state, content_hash FROM news_items WHERE news_id = ?", (item.news_id,)
    ).fetchone()
    if row is not None:
        if row["manual_state"] in ("hidden", "edited"):
            return "preserved"
        if row["content_hash"] == item.content_hash and row["manual_state"] == "active":
            return "unchanged"
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO news_items (
                news_id, classification, category, title, summary, published_on, event_date,
                confidence, source_message_ids, public_only, machine_processed, verified,
                manual_state, origin, prompt_version, model, content_hash, source_deleted,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (news_id) DO UPDATE SET
                classification = excluded.classification,
                category = excluded.category,
                title = excluded.title,
                summary = excluded.summary,
                published_on = excluded.published_on,
                event_date = excluded.event_date,
                confidence = excluded.confidence,
                source_message_ids = excluded.source_message_ids,
                public_only = excluded.public_only,
                prompt_version = excluded.prompt_version,
                model = excluded.model,
                content_hash = excluded.content_hash,
                updated_at = excluded.updated_at
            """,
            (
                item.news_id,
                item.classification,
                item.category,
                item.title,
                item.summary,
                item.published_on.isoformat() if item.published_on else None,
                item.event_date.isoformat() if getattr(item, "event_date", None) else None,
                getattr(item, "confidence", None),
                json.dumps(list(item.source_message_ids)),
                int(item.public_only),
                1,
                int(item.verified),
                getattr(item, "manual_state", "active"),
                item.origin,
                getattr(item, "prompt_version", None),
                getattr(item, "model", None),
                item.content_hash,
                0,
                stamp,
                stamp,
            ),
        )
    return "updated" if row is not None else "inserted"


def set_manual_state(conn: sqlite3.Connection, *, news_id: str, state: str, now: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE news_items SET manual_state = ?, updated_at = ? WHERE news_id = ?",
            (state, now or _utcnow(), news_id),
        )


def mark_news_source_deleted(conn: sqlite3.Connection, *, news_id: str, now: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE news_items SET source_deleted = 1, updated_at = ? WHERE news_id = ?",
            (now or _utcnow(), news_id),
        )


def public_news_items(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        """
        SELECT * FROM news_items
         WHERE classification = 'news'
           AND source_deleted = 0
           AND manual_state != 'hidden'
         ORDER BY published_on, news_id
        """
    ).fetchall()
    return tuple(dict(row) for row in rows)


def announcement_news_items(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Public announcement items for the «Что пересылали» ranking.

    Same visibility contract as :func:`public_news_items` (not deleted, not
    hidden), but for the ``announcement`` classification the LLM/manual desk
    already produced — the ranking never re-classifies a post on its own.
    """

    rows = conn.execute(
        """
        SELECT * FROM news_items
         WHERE classification = 'announcement'
           AND source_deleted = 0
           AND manual_state != 'hidden'
         ORDER BY published_on, news_id
        """
    ).fetchall()
    return tuple(dict(row) for row in rows)


def news_items_by_source_ids(conn: sqlite3.Connection, message_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
    wanted = set(message_ids)
    found: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM news_items"):
        ids = set(json.loads(row["source_message_ids"]))
        if ids & wanted:
            found.append(dict(row))
    return tuple(found)


def find_news_for_sources(conn: sqlite3.Connection, message_ids: Sequence[str]) -> dict[str, Any] | None:
    """Deduplicate by source ids, not by title (docs/REQUIREMENTS.md section 8)."""

    wanted = set(message_ids)
    for row in conn.execute("SELECT * FROM news_items ORDER BY created_at"):
        if set(json.loads(row["source_message_ids"])) == wanted:
            return dict(row)
    return None


# --------------------------------------------------------------------------- #
# Deferred LLM jobs (written by the extractor, retried at news-sync start)
# --------------------------------------------------------------------------- #

def deferred_jobs_list(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Pending deferred jobs, oldest first — the order the consumer retries."""

    rows = conn.execute("SELECT * FROM deferred_jobs ORDER BY created_at, job_id").fetchall()
    return tuple(dict(row) for row in rows)


def deferred_jobs_delete(conn: sqlite3.Connection, job_id: str) -> None:
    """Clear a deferred job once its batch went through (or has nothing left)."""

    with transaction(conn):
        conn.execute("DELETE FROM deferred_jobs WHERE job_id = ?", (job_id,))


# --------------------------------------------------------------------------- #
# Runs, last-good payloads, releases, issues
# --------------------------------------------------------------------------- #

def start_source_run(
    conn: sqlite3.Connection,
    *,
    source: str,
    logical_date: str,
    cutoff: str,
    started_at: str | None = None,
) -> tuple[str, bool]:
    """Create or reuse the logical run for (source, MSK day).

    The idempotency key is (source, logical_date): a second attempt inside the
    same logical run reuses the row instead of duplicating the snapshot.
    """

    run_id = f"run_{logical_date}_{source}"
    stamp = started_at or _utcnow()
    row = conn.execute("SELECT run_id FROM source_runs WHERE source = ? AND logical_date = ?", (source, logical_date)).fetchone()
    if row is not None:
        with transaction(conn):
            conn.execute(
                "UPDATE source_runs SET last_attempt_at = ?, status = 'running' WHERE run_id = ?",
                (stamp, run_id),
            )
        return run_id, False
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO source_runs (run_id, source, logical_date, cutoff, started_at, status, last_attempt_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (run_id, source, logical_date, cutoff, stamp, stamp),
        )
    return run_id, True


def finish_source_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    counts: UpsertCounts | None = None,
    data_as_of: str | None = None,
    coverage: str | None = None,
    error: str | None = None,
    now: str | None = None,
) -> None:
    stamp = now or _utcnow()
    counts = counts or UpsertCounts()
    row = conn.execute("SELECT error_count FROM source_runs WHERE run_id = ?", (run_id,)).fetchone()
    previous_errors = int(row["error_count"]) if row else 0
    successful = status == "ok"
    with transaction(conn):
        conn.execute(
            """
            UPDATE source_runs
               SET finished_at = ?, status = ?, data_as_of = COALESCE(?, data_as_of),
                   coverage = COALESCE(?, coverage),
                   new_rows = ?, changed_rows = ?, deleted_rows = ?,
                   error = ?, error_count = ?,
                   last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END
             WHERE run_id = ?
            """,
            (
                stamp,
                status,
                data_as_of,
                coverage,
                counts.new,
                counts.changed,
                counts.deleted,
                error,
                previous_errors + (0 if successful else 1),
                int(successful),
                stamp,
                run_id,
            ),
        )


def set_last_good(
    conn: sqlite3.Connection,
    *,
    source: str,
    run_id: str,
    payload: Mapping[str, Any],
    data_as_of: str,
    coverage: str,
    now: str | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO last_good_snapshots (source, run_id, payload, data_as_of, coverage, stored_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source) DO UPDATE SET
                run_id = excluded.run_id,
                payload = excluded.payload,
                data_as_of = excluded.data_as_of,
                coverage = excluded.coverage,
                stored_at = excluded.stored_at
            """,
            (source, run_id, json.dumps(payload, ensure_ascii=False), data_as_of, coverage, now or _utcnow()),
        )


def last_good(conn: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM last_good_snapshots WHERE source = ?", (source,)).fetchone()
    if row is None:
        return None
    return {
        "source": row["source"],
        "run_id": row["run_id"],
        "payload": json.loads(row["payload"]),
        "data_as_of": row["data_as_of"],
        "coverage": row["coverage"],
        "stored_at": row["stored_at"],
    }


def source_runs(conn: sqlite3.Connection, source: str | None = None) -> tuple[dict[str, Any], ...]:
    if source is None:
        rows = conn.execute("SELECT * FROM source_runs ORDER BY source, logical_date").fetchall()
    else:
        rows = conn.execute("SELECT * FROM source_runs WHERE source = ? ORDER BY logical_date", (source,)).fetchall()
    return tuple(dict(row) for row in rows)


def get_cursor(conn: sqlite3.Connection, source: str) -> tuple[str, int] | None:
    row = conn.execute("SELECT cursor, revision FROM source_cursors WHERE source = ?", (source,)).fetchone()
    if row is None:
        return None
    return str(row["cursor"]), int(row["revision"])


def set_cursor(conn: sqlite3.Connection, source: str, cursor: str, *, revision: int = 1, now: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO source_cursors (source, cursor, revision, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (source) DO UPDATE SET
                cursor = excluded.cursor,
                revision = excluded.revision,
                updated_at = excluded.updated_at
            """,
            (source, cursor, revision, now or _utcnow()),
        )


def store_release(
    conn: sqlite3.Connection,
    *,
    release_id: str,
    as_of: str,
    schema_version: str,
    sections: Sequence[str],
    payload_text: str,
    built_at: str | None = None,
    make_current: bool = True,
) -> str:
    """Store one leak-checked public release. Existing releases are never rewritten."""

    stamp = built_at or _utcnow()
    existing = conn.execute("SELECT release_id FROM public_releases WHERE release_id = ?", (release_id,)).fetchone()
    if existing is not None:
        return "unchanged"
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO public_releases (release_id, built_at, as_of, schema_version, sections, payload, is_current)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (release_id, stamp, as_of, schema_version, json.dumps(list(sections)), payload_text),
        )
        if make_current:
            conn.execute("UPDATE public_releases SET is_current = 0 WHERE is_current = 1")
            conn.execute("UPDATE public_releases SET is_current = 1 WHERE release_id = ?", (release_id,))
    return "inserted"


def current_release(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM public_releases WHERE is_current = 1").fetchone()
    return dict(row) if row else None


def release(conn: sqlite3.Connection, release_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM public_releases WHERE release_id = ?", (release_id,)).fetchone()
    return dict(row) if row else None


def record_issue(
    conn: sqlite3.Connection,
    *,
    issue_id: str,
    code: str,
    severity: str,
    entity_type: str | None = None,
    entity_key: str | None = None,
    payload: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO issues (issue_id, code, severity, entity_type, entity_key, payload, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (issue_id, code, severity, entity_type, entity_key, json.dumps(payload or {}, ensure_ascii=False), now or _utcnow()),
        )


def issues(conn: sqlite3.Connection, *, code: str | None = None) -> tuple[dict[str, Any], ...]:
    if code is None:
        rows = conn.execute("SELECT * FROM issues ORDER BY created_at").fetchall()
    else:
        rows = conn.execute("SELECT * FROM issues WHERE code = ? ORDER BY created_at", (code,)).fetchall()
    return tuple(dict(row) for row in rows)


#: The lifecycle issues the nightly cycle records about a source. They carry the
#: logical date in their id, so a later healthy run is what closes them: the
#: diagnostics catalog cannot match a per-date id, and without this a transient
#: source failure would stay open forever.
SOURCE_LIFECYCLE_CODES: tuple[str, ...] = ("source_failed", "source_partial")


def resolve_source_issues(
    conn: sqlite3.Connection,
    *,
    source: str,
    codes: Sequence[str] = SOURCE_LIFECYCLE_CODES,
    now: str | None = None,
) -> tuple[str, ...]:
    """Close a source's earlier lifecycle issues: the source is healthy again.

    Resolving never hides history: the rows stay, the status and the audit trail
    say who closed them and why. Returns the resolved issue ids.
    """

    stamp = now or _utcnow()
    if not codes:
        return ()
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT issue_id FROM issues WHERE status = 'open' AND entity_type = 'source' AND entity_key = ? AND code IN ({placeholders})",
        (source, *codes),
    ).fetchall()
    resolved: list[str] = []
    for row in rows:
        issue_id = str(row["issue_id"])
        with transaction(conn):
            conn.execute("UPDATE issues SET status = 'resolved' WHERE issue_id = ?", (issue_id,))
        audit(
            conn,
            action="issue.resolved",
            actor="nightly",
            entity_type="issue",
            entity_key=issue_id,
            detail={"reason": "источник снова читается успешно"},
            now=stamp,
        )
        resolved.append(issue_id)
    return tuple(resolved)


def audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: str | None = None,
    entity_type: str | None = None,
    entity_key: str | None = None,
    detail: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO audit_log (at, actor, action, entity_type, entity_key, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (now or _utcnow(), actor, action, entity_type, entity_key, json.dumps(detail or {}, ensure_ascii=False)),
        )


def msk_logical_date(instant: datetime) -> str:
    return instant.astimezone(MSK).date().isoformat()


# --------------------------------------------------------------------------- #
# Report archive snapshots (R01)
# --------------------------------------------------------------------------- #

def record_report_snapshot(
    conn: sqlite3.Connection,
    *,
    report_id: str,
    period_label: str,
    version: int,
    published_at: str,
    payload: Mapping[str, Any],
    original_text: str | None = None,
    original_url: str | None = None,
) -> str:
    """Store one immutable version of a period report.

    A version is written exactly once. Re-recording an identical version is a
    no-op; re-recording a *different* payload under the same ``(period, version)``
    is refused, so a fixed snapshot can never be rewritten in place — a change
    must arrive as a higher version (R01).
    """

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    existing = conn.execute(
        "SELECT payload, original_text, original_url FROM report_snapshots WHERE period_label = ? AND version = ?",
        (period_label, version),
    ).fetchone()
    if existing is not None:
        if existing["payload"] == text and existing["original_text"] == original_text and existing["original_url"] == original_url:
            return "unchanged"
        raise ValueError(
            f"report version {period_label} v{version} already exists and is immutable; record a new version"
        )
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO report_snapshots (report_id, period_label, version, published_at, payload, original_text, original_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (report_id, period_label, version, published_at, text, original_text, original_url),
        )
    return "inserted"


def report_versions(conn: sqlite3.Connection, period_label: str) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        "SELECT * FROM report_snapshots WHERE period_label = ? ORDER BY version", (period_label,)
    ).fetchall()
    return tuple(dict(row) for row in rows)


def latest_report_snapshot(conn: sqlite3.Connection, period_label: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM report_snapshots WHERE period_label = ? ORDER BY version DESC LIMIT 1", (period_label,)
    ).fetchone()
    return dict(row) if row else None


def next_report_version(conn: sqlite3.Connection, period_label: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS latest FROM report_snapshots WHERE period_label = ?", (period_label,)
    ).fetchone()
    latest = row["latest"] if row else None
    return int(latest or 0) + 1


def report_period_labels(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute("SELECT DISTINCT period_label FROM report_snapshots ORDER BY period_label").fetchall()
    return tuple(row["period_label"] for row in rows)


# --------------------------------------------------------------------------- #
# Reversible per-operation report exclusions (docs/ALLOCATION.md section 4)
# --------------------------------------------------------------------------- #

def active_report_exclusions(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Ids of operations currently excluded from the report (restored ones are not)."""

    rows = conn.execute(
        "SELECT transaction_id FROM finance_report_exclusions WHERE restored_at IS NULL ORDER BY excluded_at, transaction_id"
    ).fetchall()
    return tuple(str(row["transaction_id"]) for row in rows)


def report_exclusions(conn: sqlite3.Connection, *, include_restored: bool = False) -> tuple[dict[str, Any], ...]:
    clause = "" if include_restored else "WHERE restored_at IS NULL"
    rows = conn.execute(
        f"SELECT * FROM finance_report_exclusions {clause} ORDER BY excluded_at, transaction_id"
    ).fetchall()
    return tuple(dict(row) for row in rows)


def exclude_operation_from_report(
    conn: sqlite3.Connection,
    *,
    transaction_id: str,
    actor: str | None,
    reason: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    """Exclude one operation from the report, reversibly.

    The row is created or re-activated in place; the source transaction is never
    modified, so the immutable original survives (section 6.1 forbids a local
    money correction — this only changes what the *report* counts).
    """

    stamp = now or _utcnow()
    existing = conn.execute(
        "SELECT restored_at FROM finance_report_exclusions WHERE transaction_id = ?", (transaction_id,)
    ).fetchone()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO finance_report_exclusions (transaction_id, excluded_at, actor, reason, restored_at, restored_by)
            VALUES (?, ?, ?, ?, NULL, NULL)
            ON CONFLICT (transaction_id) DO UPDATE SET
                excluded_at = excluded.excluded_at,
                actor = excluded.actor,
                reason = excluded.reason,
                restored_at = NULL,
                restored_by = NULL
            """,
            (str(transaction_id), stamp, actor, str(reason or "")),
        )
    return {
        "transaction_id": str(transaction_id),
        "excluded_at": stamp,
        "reactivated": existing is not None and existing["restored_at"] is not None,
    }


def restore_operation_to_report(
    conn: sqlite3.Connection,
    *,
    transaction_id: str,
    actor: str | None,
    now: str | None = None,
) -> dict[str, Any]:
    """Undo an exclusion: the operation returns to the report, the row stays."""

    stamp = now or _utcnow()
    row = conn.execute(
        "SELECT restored_at FROM finance_report_exclusions WHERE transaction_id = ?", (transaction_id,)
    ).fetchone()
    if row is None:
        raise ValueError("операция не исключалась из отчёта")
    if row["restored_at"] is not None:
        return {"transaction_id": str(transaction_id), "restored": False, "already": True}
    with transaction(conn):
        conn.execute(
            "UPDATE finance_report_exclusions SET restored_at = ?, restored_by = ? WHERE transaction_id = ?",
            (stamp, actor, str(transaction_id)),
        )
    return {"transaction_id": str(transaction_id), "restored": True, "restored_at": stamp}
