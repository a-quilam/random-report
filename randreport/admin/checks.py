"""Verification of an entity *at a revision* (docs/REQUIREMENTS.md section 4; A02).

A check is never a bare flag. It is stored together with

* the entity it belongs to (type + key),
* the hash of the entity's normalised *significant* fields, and
* the version of the rules (including the mapping version) that produced it.

Reading a check therefore compares the stored revision with the current one. If
the data, the classification, the privacy mapping or the methodology moved on,
the check no longer applies: it is reported as ``stale`` rather than silently
staying green. A manual "verified" on an entity with an open discrepancy is
recorded, but the discrepancy stays open and the state stays ``conflict``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..store.db import transaction

RULES_VERSION = "checks-1"

OPERATIONAL = "operational"
VERIFIED = "verified"
CONFLICT = "conflict"
NO_DATA = "no_data"
STALE = "stale"

STATES = (OPERATIONAL, VERIFIED, CONFLICT, NO_DATA, STALE)

ENTITY_TYPES = (
    "finance_transaction",
    "finance_category",
    "calendar_occurrence",
    "channel_snapshot",
    "news_item",
    "report_period",
    "period",
    "historical_conflict",
    "historical_observation",
)


@dataclass(frozen=True)
class Check:
    check_id: str
    entity_type: str
    entity_key: str
    revision_hash: str
    rules_version: str
    state: str
    checked_at: str
    note: str | None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mapping_version(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(f"SELECT version FROM {table} ORDER BY created_at DESC, version DESC LIMIT 1").fetchone()
    return str(row["version"]) if row else "unmapped"


def _row(conn: sqlite3.Connection, sql: str, args: Iterable[Any]) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(args)).fetchone()


def _fields(row: sqlite3.Row | None, names: Iterable[str]) -> dict[str, Any]:
    if row is None:
        return {}
    return {name: row[name] for name in names if name in row.keys()}


def revision_of(conn: sqlite3.Connection, entity_type: str, entity_key: str) -> str | None:
    """The current revision hash of an entity, or ``None`` when it does not exist."""

    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown entity type {entity_type!r}")
    if entity_type == "finance_transaction":
        row = _row(conn, "SELECT * FROM finance_transactions WHERE transaction_id = ?", (entity_key,))
        if row is None:
            return None
        classification = _row(
            conn,
            "SELECT economic_class, mapping_version FROM finance_allocations_or_classification WHERE transaction_id = ? ORDER BY mapping_version DESC LIMIT 1",
            (entity_key,),
        )
        return _digest(
            {
                "row": _fields(
                    row,
                    (
                        "amount_kopecks",
                        "occurred_on",
                        "source_account",
                        "target_account",
                        "category_id",
                        "currency",
                        "pair_id",
                        "pair_kind",
                        "reimbursement_of",
                        "is_planned",
                        "deleted",
                        "source_revision",
                    ),
                ),
                "classification": _fields(classification, ("economic_class", "mapping_version")),
                "category_mapping": _mapping_version(conn, "category_mapping_versions"),
            }
        )
    if entity_type == "finance_category":
        rows = conn.execute(
            "SELECT transaction_id, amount_kopecks, occurred_on, deleted, source_revision FROM finance_transactions WHERE category_id = ? ORDER BY transaction_id",
            (entity_key,),
        ).fetchall()
        if not rows:
            return None
        return _digest(
            {
                "category": entity_key,
                "rows": [dict(row) for row in rows],
                "category_mapping": _mapping_version(conn, "category_mapping_versions"),
            }
        )
    if entity_type == "calendar_occurrence":
        calendar_id, _, occurrence_id = entity_key.partition("|")
        row = _row(
            conn,
            "SELECT * FROM calendar_occurrences WHERE calendar_id = ? AND occurrence_id = ?",
            (calendar_id, occurrence_id),
        )
        if row is None:
            return None
        return _digest(
            {
                "row": _fields(
                    row,
                    (
                        "start_utc",
                        "end_utc",
                        "all_day",
                        "subcalendar_ids",
                        "title",
                        "raw_type",
                        "revision",
                        "deleted",
                        "source_present",
                        "local_archive",
                    ),
                ),
                "event_mapping": _mapping_version(conn, "event_type_mapping"),
            }
        )
    if entity_type == "channel_snapshot":
        row = _row(conn, "SELECT * FROM channel_snapshots WHERE on_date = ?", (entity_key,))
        if row is None:
            return None
        return _digest(_fields(row, ("subscribers", "source", "revision", "received_at")))
    if entity_type == "news_item":
        row = _row(conn, "SELECT * FROM news_items WHERE news_id = ?", (entity_key,))
        if row is None:
            return None
        return _digest(
            _fields(
                row,
                (
                    "content_hash",
                    "manual_state",
                    "title",
                    "summary",
                    "category",
                    "classification",
                    "source_message_ids",
                    "source_deleted",
                    "verified",
                ),
            )
        )
    if entity_type == "report_period":
        row = _row(
            conn,
            "SELECT version, payload FROM report_snapshots WHERE period_label = ? ORDER BY version DESC LIMIT 1",
            (entity_key,),
        )
        if row is None:
            return None
        return _digest({"version": row["version"], "payload": json.loads(row["payload"])})
    if entity_type == "historical_conflict":
        row = _row(conn, "SELECT * FROM historical_conflicts WHERE conflict_id = ?", (entity_key,))
        if row is None:
            return None
        return _digest(_fields(row, ("status", "published_value", "computed_value", "difference", "notes")))
    if entity_type == "historical_observation":
        row = _row(conn, "SELECT * FROM historical_observations WHERE observation_id = ?", (entity_key,))
        if row is None:
            return None
        return _digest(_fields(row, ("metric", "period_from", "period_to", "value", "provenance", "coverage", "status")))
    # period: the published picture of a period, whichever plane carries it
    snapshot = _row(
        conn,
        "SELECT version, payload FROM report_snapshots WHERE period_label = ? ORDER BY version DESC LIMIT 1",
        (entity_key,),
    )
    release = _row(
        conn,
        "SELECT release_id, payload FROM public_releases WHERE is_current = 1 ORDER BY built_at DESC LIMIT 1",
        (),
    )
    if snapshot is None and release is None:
        return None
    return _digest(
        {
            "period": entity_key,
            "snapshot": None if snapshot is None else {"version": snapshot["version"], "payload": json.loads(snapshot["payload"])},
            "release": None if release is None else {"release_id": release["release_id"], "payload": json.loads(release["payload"])},
        }
    )


def check_for(conn: sqlite3.Connection, entity_type: str, entity_key: str) -> Check | None:
    row = _row(
        conn,
        "SELECT * FROM checks WHERE entity_type = ? AND entity_key = ?",
        (entity_type, entity_key),
    )
    if row is None:
        return None
    return Check(
        check_id=str(row["check_id"]),
        entity_type=str(row["entity_type"]),
        entity_key=str(row["entity_key"]),
        revision_hash=str(row["revision_hash"]),
        rules_version=str(row["rules_version"]),
        state=str(row["state"]),
        checked_at=str(row["checked_at"]),
        note=row["note"],
    )


def all_checks(conn: sqlite3.Connection, *, entity_type: str | None = None) -> tuple[Check, ...]:
    if entity_type is None:
        rows = conn.execute("SELECT * FROM checks ORDER BY entity_type, entity_key").fetchall()
    else:
        rows = conn.execute("SELECT * FROM checks WHERE entity_type = ? ORDER BY entity_key", (entity_type,)).fetchall()
    return tuple(
        Check(
            check_id=str(row["check_id"]),
            entity_type=str(row["entity_type"]),
            entity_key=str(row["entity_key"]),
            revision_hash=str(row["revision_hash"]),
            rules_version=str(row["rules_version"]),
            state=str(row["state"]),
            checked_at=str(row["checked_at"]),
            note=row["note"],
        )
        for row in rows
    )


def open_issues(conn: sqlite3.Connection, *, entity_type: str | None = None, entity_key: str | None = None) -> tuple[dict[str, Any], ...]:
    sql = "SELECT * FROM issues WHERE status = 'open'"
    args: list[Any] = []
    if entity_type is not None:
        sql += " AND entity_type = ?"
        args.append(entity_type)
    if entity_key is not None:
        sql += " AND entity_key = ?"
        args.append(entity_key)
    return tuple(dict(row) for row in conn.execute(sql + " ORDER BY created_at", tuple(args)))


def effective_state(conn: sqlite3.Connection, entity_type: str, entity_key: str) -> str:
    """The state a viewer must see now — never a stale "verified"."""

    current = revision_of(conn, entity_type, entity_key)
    check = check_for(conn, entity_type, entity_key)
    if current is None:
        return NO_DATA
    if check is None:
        return OPERATIONAL
    if check.rules_version != RULES_VERSION or check.revision_hash != current:
        return STALE
    return check.state


def verify(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_key: str,
    actor: str,
    note: str | None = None,
    now: str | None = None,
) -> Check:
    """Record a manual verification at the entity's current revision.

    Open discrepancies are not cleared: the state becomes ``conflict`` and the
    issue stays open (section 4).
    """

    current = revision_of(conn, entity_type, entity_key)
    stamp = now or _utcnow()
    if current is None:
        return _write(conn, entity_type=entity_type, entity_key=entity_key, state=NO_DATA, revision=current, actor=actor, note=note or "нет данных для проверки", stamp=stamp)
    issues = open_issues(conn, entity_type=entity_type, entity_key=entity_key)
    if issues:
        codes = ", ".join(sorted({str(issue["code"]) for issue in issues}))
        return _write(
            conn,
            entity_type=entity_type,
            entity_key=entity_key,
            state=CONFLICT,
            revision=current,
            actor=actor,
            note=f"проверка записана, но расхождение остаётся открытым: {codes}",
            stamp=stamp,
        )
    return _write(conn, entity_type=entity_type, entity_key=entity_key, state=VERIFIED, revision=current, actor=actor, note=note, stamp=stamp)


def _write(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_key: str,
    state: str,
    revision: str | None,
    actor: str,
    note: str | None,
    stamp: str,
) -> Check:
    check_id = f"chk_{entity_type}_{hashlib.sha256(f'{entity_type}|{entity_key}'.encode()).hexdigest()[:16]}"
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO checks (check_id, entity_type, entity_key, revision_hash, rules_version, state, checked_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (check_id) DO UPDATE SET
                revision_hash = excluded.revision_hash,
                rules_version = excluded.rules_version,
                state = excluded.state,
                checked_at = excluded.checked_at,
                note = excluded.note
            """,
            (check_id, entity_type, entity_key, revision or "", RULES_VERSION, state, stamp, note),
        )
        conn.execute(
            "INSERT INTO audit_log (at, actor, action, entity_type, entity_key, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (stamp, actor, "check.verified" if state == VERIFIED else f"check.{state}", entity_type, entity_key, json.dumps({"note": note}, ensure_ascii=False)),
        )
    return Check(
        check_id=check_id,
        entity_type=entity_type,
        entity_key=entity_key,
        revision_hash=revision or "",
        rules_version=RULES_VERSION,
        state=state,
        checked_at=stamp,
        note=note,
    )


def invalidate(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_key: str | None = None,
    cause: str,
    detail: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> int:
    """Drop the checks that a change actually invalidates, and log why.

    Only the matching entities are touched: a category-mapping change resets the
    finance and period checks, while an unrelated channel snapshot keeps its own.
    """

    stamp = now or _utcnow()
    sql = "DELETE FROM checks WHERE entity_type = ?"
    args: list[Any] = [entity_type]
    if entity_key is not None:
        sql += " AND entity_key = ?"
        args.append(entity_key)
    with transaction(conn):
        cursor = conn.execute(sql, tuple(args))
        affected = int(cursor.rowcount)
        conn.execute(
            "INSERT INTO check_invalidations (at, entity_type, entity_key, cause, rules_version, affected, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stamp, entity_type, entity_key, cause, RULES_VERSION, affected, json.dumps(dict(detail or {}), ensure_ascii=False)),
        )
    return affected


def invalidate_for_mapping(conn: sqlite3.Connection, *, kind: str, version: str, actor: str, now: str | None = None) -> dict[str, int]:
    """Category vs calendar mapping changes invalidate different checks."""

    if kind == "category":
        affected = {
            "finance_transaction": invalidate(conn, entity_type="finance_transaction", cause="mapping", detail={"kind": kind, "version": version, "actor": actor}, now=now),
            "finance_category": invalidate(conn, entity_type="finance_category", cause="mapping", detail={"kind": kind, "version": version}, now=now),
            "period": invalidate(conn, entity_type="period", cause="mapping", detail={"kind": kind, "version": version}, now=now),
        }
    elif kind == "event":
        affected = {
            "calendar_occurrence": invalidate(conn, entity_type="calendar_occurrence", cause="mapping", detail={"kind": kind, "version": version, "actor": actor}, now=now),
            "period": invalidate(conn, entity_type="period", cause="mapping", detail={"kind": kind, "version": version}, now=now),
        }
    else:
        raise ValueError("mapping kind must be 'category' or 'event'")
    return affected


def invalidate_for_source(conn: sqlite3.Connection, *, source: str, entity_type: str, entity_key: str | None = None, now: str | None = None) -> int:
    return invalidate(conn, entity_type=entity_type, entity_key=entity_key, cause="source", detail={"source": source}, now=now)


def invalidate_for_news(conn: sqlite3.Connection, *, news_id: str, actor: str, now: str | None = None) -> int:
    return invalidate(conn, entity_type="news_item", entity_key=news_id, cause="news", detail={"actor": actor}, now=now)


def invalidate_for_methodology(conn: sqlite3.Connection, *, version: str, actor: str, now: str | None = None) -> dict[str, int]:
    return {
        "period": invalidate(conn, entity_type="period", cause="methodology", detail={"version": version, "actor": actor}, now=now),
        "report_period": invalidate(conn, entity_type="report_period", cause="methodology", detail={"version": version, "actor": actor}, now=now),
    }


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts by *effective* state, for the overview panel."""

    counts = {state: 0 for state in STATES}
    for check in all_checks(conn):
        current = revision_of(conn, check.entity_type, check.entity_key)
        if current is None:
            counts[NO_DATA] += 1
        elif check.revision_hash != current or check.rules_version != RULES_VERSION:
            counts[STALE] += 1
        else:
            counts[check.state] = counts.get(check.state, 0) + 1
    return counts


def invalidations(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    rows = conn.execute("SELECT * FROM check_invalidations ORDER BY invalidation_id DESC").fetchall()
    return tuple(dict(row) for row in rows)
