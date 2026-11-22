"""The editorial desk's read/write surface (docs/REQUIREMENTS.md section 4).

One module per desk panel, all of them reading the same private store:

* **overview** — the last nightly run, each source's state, open problems, what
  still needs a decision, and which periods are not closed yet;
* **money** — the source operations *inside the public contour* only, with the
  row card and an annotation. Money itself is read-only: a local money
  correction does not exist in this product (section 6.1, "Исправление
  финансов — только в Zenmoney");
* **events** — source, stable id, time, type, publicity and the local mapping,
  which can be changed (with audit) but never re-typed;
* **channel** — snapshots and the gaps between them;
* **settings** — the public contour map, categories by id, calendar types, the
  donor rule, allowed news sources, administrators and the schedule, with a
  change log that is reversible by *new* versions only.

Nothing here writes money. Nothing here shows a raw bank comment, a payment
requisite or a private account's operations.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from ..ops import status as ops_status
from ..ops.schedule import RUN_HOUR_MSK, SYSTEMD_CALENDAR
from ..store import repository as repo
from ..store.db import transaction
from . import checks, history_admin, members

MONEY_LIMIT = 200
AUDIT_LIMIT = 100


class DeskError(ValueError):
    """A desk action that must not be applied."""


class MoneyReadOnly(DeskError):
    """Money fields are never editable through the desk."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(data: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(data or {})


# --------------------------------------------------------------------------- #
# overview
# --------------------------------------------------------------------------- #

def source_detail(conn: sqlite3.Connection, source: str, *, now: datetime | None = None) -> dict[str, Any]:
    """One source's runs, coverage, last validated payload and open problems."""

    moment = now or datetime.now(timezone.utc)
    state = ops_status.source_status(conn, source, now=moment)
    runs = [row for row in repo.source_runs(conn, source)]
    coverage = [
        {
            "grain": str(row["period_grain"]),
            "from": str(row["period_from"]),
            "to": str(row["period_to"]),
            "coverage": str(row["coverage"]),
            "data_as_of": row["data_as_of"],
            "provenance": row["provenance"],
        }
        for row in conn.execute(
            "SELECT * FROM source_coverage WHERE source = ? ORDER BY period_from", (source,)
        ).fetchall()
    ]
    good = repo.last_good(conn, source)
    issues = [
        {"issue_id": str(row["issue_id"]), "code": str(row["code"]), "severity": str(row["severity"]), "entity_key": row["entity_key"], "status": str(row["status"])}
        for row in conn.execute(
            "SELECT * FROM issues WHERE status != 'resolved' AND (entity_key = ? OR code IN ('source_unreliable', 'coverage_gap')) ORDER BY created_at",
            (source,),
        ).fetchall()
    ]
    return {
        "source": source,
        "exists": bool(runs) or good is not None,
        "status": {
            "status": state.status,
            "freshness": state.freshness,
            "last_attempt_at": state.last_attempt_at,
            "last_success_at": state.last_success_at,
            "data_as_of": state.data_as_of,
            "coverage": state.coverage,
            "error_count": state.error_count,
            "last_error": state.last_error,
            "has_last_good": state.has_last_good,
        },
        "runs": runs,
        "coverage": coverage,
        "last_good": None if good is None else {"run_id": good["run_id"], "data_as_of": good["data_as_of"], "coverage": good["coverage"], "stored_at": good["stored_at"]},
        "issues": issues,
        "freshness_note": (
            "Раздел отдаёт последнюю проверенную версию с пометкой «устарело»; ноль не подставляется."
            if state.freshness != "fresh"
            else "Данные свежие."
        ),
    }


def source_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    names = {str(row["source"]) for row in conn.execute("SELECT DISTINCT source FROM source_runs")}
    names |= {str(row["source"]) for row in conn.execute("SELECT DISTINCT source FROM source_coverage")}
    names |= {str(row["source"]) for row in conn.execute("SELECT DISTINCT source FROM last_good_snapshots")}
    return tuple(sorted(names))


def unclosed_periods(conn: sqlite3.Connection, *, today: date | None = None) -> tuple[dict[str, Any], ...]:
    """Periods that carry data but have no immutable closed version yet."""

    months: set[str] = set()
    for row in conn.execute("SELECT DISTINCT substr(occurred_on, 1, 7) AS month FROM finance_transactions WHERE deleted = 0"):
        if row["month"]:
            months.add(str(row["month"]))
    for row in conn.execute("SELECT DISTINCT substr(start_utc, 1, 7) AS month FROM calendar_occurrences WHERE deleted = 0"):
        if row["month"]:
            months.add(str(row["month"]))
    for row in conn.execute("SELECT DISTINCT substr(published_on, 1, 7) AS month FROM news_items"):
        if row["month"]:
            months.add(str(row["month"]))
    moment = today or datetime.now(timezone.utc).date()
    months.add(f"{moment.year:04d}-{moment.month:02d}")
    closed = {str(row["period_label"]) for row in conn.execute("SELECT period_label FROM report_snapshots")}
    draft_closed = {
        str(row["period_label"])
        for row in conn.execute("SELECT period_label FROM post_drafts WHERE status = 'closed'")
    }
    out = []
    for label in sorted(months):
        snapshot = conn.execute(
            "SELECT MAX(version) AS v FROM report_snapshots WHERE period_label = ?", (label,)
        ).fetchone()
        out.append(
            {
                "period": label,
                "closed": label in closed or label in draft_closed,
                "version": int(snapshot["v"]) if snapshot and snapshot["v"] is not None else None,
                "draft_status": _draft_status(conn, label),
            }
        )
    return tuple(out)


def _draft_status(conn: sqlite3.Connection, period_label: str) -> str:
    row = conn.execute("SELECT status FROM post_drafts WHERE period_label = ?", (period_label,)).fetchone()
    return str(row["status"]) if row else "none"


def overview(conn: sqlite3.Connection, *, index: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """Everything the front page of the desk states, in one object."""

    moment = now or datetime.now(timezone.utc)
    runs = repo.source_runs(conn)
    sources = sorted({str(row["source"]) for row in runs})
    statuses = {source: ops_status.source_status(conn, source, now=moment) for source in sources}
    latest: dict[str, dict[str, Any]] = {}
    for row in runs:
        latest[str(row["source"])] = {
            "run_id": row["run_id"],
            "logical_date": row["logical_date"],
            "status": row["status"],
            "last_success_at": row["last_success_at"],
            "coverage": row["coverage"],
            "error_count": int(row["error_count"]),
        }
    issues = _issue_shortlist(conn)
    return {
        "as_of": moment.isoformat(),
        "last_run": max((str(row["last_attempt_at"]) for row in runs), default=None),
        "sources": [
            {
                "source": source,
                "freshness": statuses[source].freshness,
                "status": statuses[source].status,
                "last_success_at": statuses[source].last_success_at,
                "coverage": statuses[source].coverage,
                "has_last_good": statuses[source].has_last_good,
                "error_count": statuses[source].error_count,
                "last_error": statuses[source].last_error,
                "latest": latest.get(source),
            }
            for source in sources
        ],
        "problem_count": len(issues),
        "problems": issues,
        "checks": checks.summary(conn),
        "unclosed_periods": [period for period in unclosed_periods(conn) if not period["closed"]],
        "release": _release_summary(conn, index),
        "members": len(members.active_members(conn)),
        "history": {
            "unresolved": len(history_admin.unresolved(conn)),
            "conflicts_open": len(history_admin.conflicts(conn, status="open")),
            "gaps": [gap["year"] for gap in history_admin.gaps(conn)],
        },
    }


def _issue_shortlist(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute(
        "SELECT issue_id, code, severity, entity_type, entity_key, status, payload FROM issues WHERE status != 'resolved' ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, created_at LIMIT ?",
        (limit,),
    ).fetchall():
        out.append(
            {
                "issue_id": str(row["issue_id"]),
                "code": str(row["code"]),
                "severity": str(row["severity"]),
                "entity_type": row["entity_type"],
                "entity_key": row["entity_key"],
                "status": str(row["status"]),
                "payload": json.loads(row["payload"] or "{}"),
            }
        )
    return out


def _release_summary(conn: sqlite3.Connection, index: Any) -> dict[str, Any] | None:
    row = repo.current_release(conn)
    if row is None:
        return None
    return {
        "release_id": str(row["release_id"]),
        "as_of": str(row["as_of"]),
        "built_at": str(row["built_at"]),
        "sections": json.loads(row["sections"]),
    }


# --------------------------------------------------------------------------- #
# money (read-only)
# --------------------------------------------------------------------------- #

def _policy(conn: sqlite3.Connection):
    from ..web.build import policy_from_store

    return policy_from_store(conn)


def money_rows(
    conn: sqlite3.Connection,
    *,
    period: str | None = None,
    text: str | None = None,
    category_id: str | None = None,
    limit: int = MONEY_LIMIT,
) -> tuple[dict[str, Any], ...]:
    """Source operations *inside the public contour*; private ones are not listed."""

    policy = _policy(conn)
    manifest = _manifest(conn)
    labels = {str(key): str(value) for key, value in (manifest.get("category_labels") or {}).items()}
    classes = {str(key): str(value) for key, value in (manifest.get("category_classes") or {}).items()}
    public_accounts = set(policy.public_accounts)
    rows = conn.execute(
        """
        SELECT t.*, c.title AS source_category_title, c.raw_title AS source_category_raw,
               a.note AS annotation
          FROM finance_transactions AS t
          LEFT JOIN finance_categories AS c ON c.category_id = t.category_id
          LEFT JOIN finance_annotations AS a ON a.transaction_id = t.transaction_id
         WHERE t.deleted = 0
         ORDER BY t.occurred_on DESC, t.transaction_id
         LIMIT ?
        """,
        (max(limit, 1) * 5,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        source = str(row["source_account"] or "")
        target = str(row["target_account"] or "")
        accounts = {account for account in (source, target) if account}
        if accounts and not accounts <= public_accounts:
            # an operation touching an account outside the public contour is not shown
            continue
        category = None if row["category_id"] is None else str(row["category_id"])
        if period and not str(row["occurred_on"]).startswith(period):
            continue
        if category_id and category != category_id:
            continue
        label = labels.get(category or "", None)
        haystack = " ".join(
            str(part or "")
            for part in (row["transaction_id"], label, row["source_category_title"], row["source_category_raw"], category, row["occurred_on"])
        ).lower()
        if text and text.lower() not in haystack:
            continue
        out.append(
            {
                "transaction_id": str(row["transaction_id"]),
                "occurred_on": str(row["occurred_on"]),
                "amount_kopecks": int(row["amount_kopecks"]),
                "direction": "out" if source else "in",
                "account": source or target,
                "category_key": category,
                "category_label": label or "без сопоставления",
                "economic_class": classes.get(category or "", "unclassified"),
                "currency": str(row["currency"]),
                "note": row["annotation"],
                "check_state": checks.effective_state(conn, "finance_transaction", str(row["transaction_id"])),
            }
        )
        if len(out) >= max(limit, 1):
            break
    return tuple(out)


def money_card(conn: sqlite3.Connection, transaction_id: str) -> dict[str, Any] | None:
    for row in money_rows(conn, limit=MONEY_LIMIT):
        if row["transaction_id"] == transaction_id:
            return row
    return None


def annotate(conn: sqlite3.Connection, *, transaction_id: str, note: str, actor: str, now: str | None = None) -> dict[str, Any]:
    """An annotation is the only thing the desk may add to a money row."""

    card = money_card(conn, transaction_id)
    if card is None:
        raise DeskError("операция не найдена в публичном контуре")
    text = str(note or "").strip()
    if not text:
        raise DeskError("аннотация не может быть пустой")
    if len(text) > 500:
        raise DeskError("аннотация длиннее 500 символов")
    stamp = now or _utcnow()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO finance_annotations (transaction_id, note, actor, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (transaction_id) DO UPDATE SET note = excluded.note, actor = excluded.actor, updated_at = excluded.updated_at
            """,
            (transaction_id, text, actor, stamp),
        )
    repo.audit(conn, action="money.annotated", actor=actor, entity_type="finance_transaction", entity_key=transaction_id, now=stamp)
    return {"transaction_id": transaction_id, "note": text}


def edit_money(*_args: Any, **_kwargs: Any) -> None:
    """Money is never edited here — the correction belongs to Zenmoney."""

    raise MoneyReadOnly(
        "Денежные поля в редакционном столе только для чтения: исправление суммы делается в Zenmoney, "
        "здесь допустимы только аннотация и сопоставление категории."
    )


def _manifest(conn: sqlite3.Connection) -> Mapping[str, Any]:
    loaded = repo.load_category_mapping(conn)
    return {} if loaded is None else dict(loaded[1])


def _event_manifest(conn: sqlite3.Connection) -> Mapping[str, Any]:
    loaded = repo.load_event_mapping(conn)
    return {} if loaded is None else dict(loaded[1])


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #

def events_rows(conn: sqlite3.Connection, *, period: str | None = None, limit: int = MONEY_LIMIT) -> tuple[dict[str, Any], ...]:
    loaded = repo.load_event_mapping(conn)
    mapping = {} if loaded is None else dict(loaded[1])
    types = {str(key): str(value) for key, value in (mapping.get("subcalendar_types") or {}).items()}
    private = {str(item) for item in (mapping.get("private_subcalendars") or ())}
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM calendar_occurrences WHERE deleted = 0 ORDER BY start_utc DESC LIMIT ?", (max(limit, 1) * 3,)
    ).fetchall():
        if period and not str(row["start_utc"]).startswith(period):
            continue
        subcalendars = [str(item) for item in json.loads(row["subcalendar_ids"])]
        event_type = types.get(subcalendars[0], "unknown") if subcalendars else "unknown"
        out.append(
            {
                "calendar_id": str(row["calendar_id"]),
                "occurrence_id": str(row["occurrence_id"]),
                "key": f"{row['calendar_id']}|{row['occurrence_id']}",
                "start_utc": str(row["start_utc"]),
                "end_utc": row["end_utc"],
                "all_day": bool(row["all_day"]),
                "subcalendars": subcalendars,
                "event_type": event_type,
                "public": event_type in ("open", "open_by_registration") and not any(item in private for item in subcalendars),
                "source_present": bool(row["source_present"]),
                "local_archive": bool(row["local_archive"]),
                "check_state": checks.effective_state(conn, "calendar_occurrence", f"{row['calendar_id']}|{row['occurrence_id']}"),
            }
        )
        if len(out) >= max(limit, 1):
            break
    return tuple(out)


def set_event_mapping(
    conn: sqlite3.Connection,
    *,
    subcalendar_id: str,
    event_type: str,
    private: bool,
    actor: str,
    now: str | None = None,
) -> str:
    """Record a *new* event-mapping version; the previous versions stay."""

    allowed = {"open", "open_by_registration", "closed", "internal_meeting", "preparation", "unknown"}
    if event_type not in allowed:
        raise DeskError(f"неизвестный тип события {event_type!r}")
    loaded = repo.load_event_mapping(conn)
    payload = {} if loaded is None else dict(loaded[1])
    types = {str(key): str(value) for key, value in (payload.get("subcalendar_types") or {}).items()}
    types[str(subcalendar_id)] = event_type
    private_ids = {str(item) for item in (payload.get("private_subcalendars") or ())}
    if private:
        private_ids.add(str(subcalendar_id))
    else:
        private_ids.discard(str(subcalendar_id))
    version = _next_version(conn, "event_type_mapping", "event-map")
    repo.record_event_mapping(conn, version=version, payload={"subcalendar_types": types, "private_subcalendars": sorted(private_ids)}, now=now)
    repo.audit(
        conn,
        action="mapping.event.updated",
        actor=actor,
        entity_type="event_type_mapping",
        entity_key=str(subcalendar_id),
        detail={"version": version, "type": event_type, "private": private},
        now=now,
    )
    checks.invalidate_for_mapping(conn, kind="event", version=version, actor=actor, now=now)
    return version


def set_category_mapping(
    conn: sqlite3.Connection,
    *,
    category_id: str,
    economic_class: str | None = None,
    label: str | None = None,
    actor: str,
    now: str | None = None,
) -> str:
    """Map a category's economic class and/or its safe display label, as a new version."""

    from ..domain.flows import EconomicClass

    known = {member.value for member in EconomicClass}
    loaded = repo.load_category_mapping(conn)
    payload = {} if loaded is None else dict(loaded[1])
    classes = {str(key): str(value) for key, value in (payload.get("category_classes") or {}).items()}
    labels = {str(key): str(value) for key, value in (payload.get("category_labels") or {}).items()}
    if economic_class is not None:
        if economic_class not in known:
            raise DeskError(f"неизвестный экономический класс {economic_class!r}")
        classes[str(category_id)] = economic_class
    if label is not None:
        text = str(label).strip()
        if not text:
            labels.pop(str(category_id), None)
        elif len(text) > 120:
            raise DeskError("отображаемое имя длиннее 120 символов")
        else:
            labels[str(category_id)] = text
    version = _next_version(conn, "category_mapping_versions", "policy")
    merged = dict(payload)
    merged["category_classes"] = classes
    merged["category_labels"] = labels
    repo.record_category_mapping(conn, version=version, payload=merged, now=now)
    repo.audit(
        conn,
        action="mapping.category.updated",
        actor=actor,
        entity_type="finance_category",
        entity_key=str(category_id),
        detail={"version": version, "economic_class": economic_class, "label": label},
        now=now,
    )
    checks.invalidate_for_mapping(conn, kind="category", version=version, actor=actor, now=now)
    return version


def _next_version(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return f"{prefix}-{int(row['n']) + 1}"


def mapping_versions(conn: sqlite3.Connection, *, kind: str) -> tuple[dict[str, Any], ...]:
    table = "category_mapping_versions" if kind == "category" else "event_type_mapping"
    rows = conn.execute(f"SELECT version, created_at, payload FROM {table} ORDER BY created_at DESC, version DESC").fetchall()
    return tuple({"version": str(row["version"]), "created_at": str(row["created_at"]), "payload": json.loads(row["payload"])} for row in rows)


def rollback_mapping(conn: sqlite3.Connection, *, kind: str, version: str, actor: str, now: str | None = None) -> str:
    """Restore a previous mapping *as a new version* — history is never rewritten."""

    versions = {row["version"]: row for row in mapping_versions(conn, kind=kind)}
    chosen = versions.get(str(version))
    if chosen is None:
        raise DeskError("нет такой версии сопоставления")
    new_version = _next_version(conn, "category_mapping_versions" if kind == "category" else "event_type_mapping", "policy" if kind == "category" else "event-map")
    if kind == "category":
        repo.record_category_mapping(conn, version=new_version, payload=chosen["payload"], now=now)
    else:
        repo.record_event_mapping(conn, version=new_version, payload=chosen["payload"], now=now)
    repo.audit(
        conn,
        action="mapping.rolled_back",
        actor=actor,
        entity_type="mapping",
        entity_key=str(version),
        detail={"kind": kind, "new_version": new_version},
        now=now,
    )
    checks.invalidate_for_mapping(conn, kind=kind, version=new_version, actor=actor, now=now)
    return new_version


# --------------------------------------------------------------------------- #
# channel
# --------------------------------------------------------------------------- #

def channel_rows(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    rows = conn.execute("SELECT * FROM channel_snapshots ORDER BY on_date").fetchall()
    out: list[dict[str, Any]] = []
    previous: date | None = None
    for row in rows:
        moment = date.fromisoformat(str(row["on_date"]))
        gap_days = None if previous is None else (moment - previous).days
        out.append(
            {
                "on_date": str(row["on_date"]),
                "subscribers": int(row["subscribers"]),
                "source": str(row["source"]),
                "revision": int(row["revision"]),
                "received_at": str(row["received_at"]),
                "gap_days": gap_days,
                "check_state": checks.effective_state(conn, "channel_snapshot", str(row["on_date"])),
            }
        )
        previous = moment
    return tuple(out)


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #

DONOR_RULE = (
    "Пока правило идентичности донатеров не восстановлено по оригинальным публикациям, публикуются только "
    "общие показатели и уже публично названные люди из архивных отчётов. Новые имена — только по явно "
    "разрешённым публичным псевдонимам; суммы по людям не показываются, если это не часть прежней методики."
)


def settings_view(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    policy = _policy(conn)
    manifest = _manifest(conn)
    news_sources = tuple(
        {"source_id": str(row["source_id"]), "channel_id": str(row["channel_id"]), "enabled": bool(row["enabled"])}
        for row in conn.execute("SELECT * FROM news_sources ORDER BY source_id")
    )
    return {
        "contour": {
            "version": policy.version,
            "public_accounts": sorted(policy.public_accounts),
            "private_accounts": sorted(policy.private_accounts),
            "quarantined_accounts": sorted(policy.quarantined_accounts),
            "quarantined_categories": sorted(policy.quarantined_categories),
            "count_visibility_threshold_kopecks": policy.count_visibility_threshold_kopecks,
        },
        "categories": sorted(
            (
                {
                    "category_id": str(key),
                    "label": str(value),
                    "economic_class": str((manifest.get("category_classes") or {}).get(key, "unclassified")),
                }
                for key, value in (manifest.get("category_labels") or {}).items()
            ),
            key=lambda item: item["category_id"],
        ),
        "unmapped_categories": sorted(
            {str(row["category_id"]) for row in conn.execute("SELECT DISTINCT category_id FROM finance_transactions WHERE category_id IS NOT NULL")}
            - {str(key) for key in (manifest.get("category_classes") or {})}
        ),
        "event_types": dict(_event_manifest(conn).get("subcalendar_types") or {}),
        "donor_rule": DONOR_RULE,
        "news_sources": news_sources,
        "members": [
            {"telegram_user_id": member.telegram_user_id, "added_at": member.added_at, "active": member.active, "note": member.note}
            for member in members.all_members(conn)
        ],
        "schedule": {
            "time": f"{RUN_HOUR_MSK:02d}:00",
            "timezone": "Europe/Moscow",
            "systemd_calendar": SYSTEMD_CALENDAR,
            "note": "Один суточный запуск; catch-up восстанавливает пропущенный цикл, ручной повтор не требуется.",
        },
        "mapping_versions": {
            "category": [row["version"] for row in mapping_versions(conn, kind="category")],
            "event": [row["version"] for row in mapping_versions(conn, kind="event")],
        },
    }


def audit_trail(conn: sqlite3.Connection, *, limit: int = AUDIT_LIMIT) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        "SELECT at, actor, action, entity_type, entity_key, detail FROM audit_log ORDER BY audit_id DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    return tuple(
        {
            "at": str(row["at"]),
            "actor": row["actor"],
            "action": str(row["action"]),
            "entity_type": row["entity_type"],
            "entity_key": row["entity_key"],
            "detail": json.loads(row["detail"] or "{}"),
        }
        for row in rows
    )


def invalidations(conn: sqlite3.Connection, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        "SELECT * FROM check_invalidations ORDER BY invalidation_id DESC LIMIT ?", (max(1, limit),)
    ).fetchall()
    return tuple(dict(row) for row in rows)


def news_sources(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"source_id": str(row["source_id"]), "channel_id": str(row["channel_id"]), "enabled": bool(row["enabled"])}
        for row in conn.execute("SELECT * FROM news_sources ORDER BY source_id")
    )


def set_news_source(conn: sqlite3.Connection, *, source_id: str, channel_id: str, enabled: bool, actor: str, now: str | None = None) -> dict[str, Any]:
    if not str(source_id).strip() or not str(channel_id).strip():
        raise DeskError("источник новостей требует идентификатор и канал")
    stamp = now or _utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO news_sources (source_id, channel_id, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT (source_id) DO UPDATE SET channel_id = excluded.channel_id, enabled = excluded.enabled",
            (str(source_id), str(channel_id), 1 if enabled else 0),
        )
    repo.audit(conn, action="settings.news_source", actor=actor, entity_type="news_source", entity_key=str(source_id), detail={"enabled": enabled}, now=stamp)
    return {"source_id": str(source_id), "channel_id": str(channel_id), "enabled": enabled}


@dataclass(frozen=True)
class DonorAlias:
    alias_id: str
    public_label: str
    allowed: bool
    note: str | None


def donor_aliases(conn: sqlite3.Connection) -> tuple[DonorAlias, ...]:
    rows = conn.execute("SELECT * FROM donor_public_aliases ORDER BY public_label").fetchall()
    return tuple(
        DonorAlias(alias_id=str(row["alias_id"]), public_label=str(row["public_label"]), allowed=bool(row["allowed"]), note=row["note"])
        for row in rows
    )


def set_donor_alias(conn: sqlite3.Connection, *, alias_id: str, public_label: str, allowed: bool, actor: str, note: str | None = None, now: str | None = None) -> DonorAlias:
    text = str(public_label or "").strip()
    if not text:
        raise DeskError("публичное имя донатера не может быть пустым")
    stamp = now or _utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO donor_public_aliases (alias_id, public_label, allowed, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (alias_id) DO UPDATE SET public_label = excluded.public_label, allowed = excluded.allowed, note = excluded.note",
            (str(alias_id), text, 1 if allowed else 0, note),
        )
    repo.audit(conn, action="settings.donor_alias", actor=actor, entity_type="donor_alias", entity_key=str(alias_id), detail={"allowed": allowed}, now=stamp)
    return DonorAlias(alias_id=str(alias_id), public_label=text, allowed=allowed, note=note)
