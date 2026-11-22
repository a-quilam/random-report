"""Historical observations, their provenance and their conflicts (section 12; H01).

The historical plane is an *archive of statements*, not a ledger. This module
keeps that separation enforceable:

* only an aggregate that a **published post** actually stated, over a **whole,
  complete** period, is a candidate for "verified fact";
* everything else — a figure from an ambiguous year, a number from a retelling,
  two overlapping internal-book variants, a value that is simply *missing* — is
  stored as an observation with an explicit non-publishable status and can never
  be promoted by a button click;
* a "missing" value is not a zero: ``0`` is a confirmed absence, ``None`` is
  "нет данных";
* the incomplete 2025 calendar stays partial: months with no published report are
  named as a gap, never filled in by interpolation or by splitting a wider
  report.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import history
from ..store import repository as repo
from ..store.db import transaction

OK = "ok"
NEEDS_SOURCE = "needs_source"
AMBIGUOUS_YEAR = "ambiguous_year"
CONFLICT = "conflict"

PROMOTABLE_STATUSES = (OK,)
PROMOTABLE_PROVENANCE = ("published",)


class HistoryError(ValueError):
    """An observation that must not become a published fact."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Observation:
    observation_id: str
    metric: str
    period_from: str | None
    period_to: str | None
    grain: str
    value: int | None
    unit: str
    source_id: str
    provenance: str
    coverage: str
    status: str
    note: str

    @property
    def period_label(self) -> str:
        if self.period_from is None:
            return "период не подтверждён"
        if self.period_to is None or self.period_to == self.period_from:
            return self.period_from
        return f"{self.period_from}..{self.period_to}"

    @property
    def promotable(self) -> bool:
        return (
            self.provenance in PROMOTABLE_PROVENANCE
            and self.status in PROMOTABLE_STATUSES
            and self.coverage == history.COMPLETE
            and self.value is not None
            and self.period_from is not None
            and self.period_to is not None
        )

    @property
    def display_value(self) -> str:
        if self.value is None:
            return "нет данных"
        return str(self.value)


# Documented evidence that is *not* a public fact (docs/source_inventory.md,
# docs/DECISIONS.md). It is kept visible and unresolved on purpose.
_UNRESOLVED: tuple[dict[str, Any], ...] = (
    {
        "metric": "subscribers",
        "period_from": "2025-09",
        "period_to": "2025-12",
        "grain": "range",
        "value": None,
        "unit": "people",
        "source_id": "tg:random_culture:growth-retelling",
        "provenance": "derived",
        "coverage": history.UNKNOWN,
        "status": NEEDS_SOURCE,
        "note": "Рост 4128→4938 относится к периоду с середины сентября, а не ко всему году; "
        "источник — пересказ, точных точек канала нет. В агрегаты не входит.",
    },
    {
        "metric": "expenses",
        "period_from": None,
        "period_to": None,
        "grain": "year",
        "value": None,
        "unit": "RUB",
        "source_id": "internal-book:variant-a",
        "provenance": "draft",
        "coverage": history.UNKNOWN,
        "status": AMBIGUOUS_YEAR,
        "note": "Два варианта внутренней книги перекрываются, и год в них не подтверждён метаданными "
        "публикации. До подтверждения — наблюдение, не факт.",
    },
    {
        "metric": "events",
        "period_from": None,
        "period_to": None,
        "grain": "range",
        "value": None,
        "unit": "events",
        "source_id": "calendar-pdf:2025",
        "provenance": "derived",
        "coverage": history.PARTIAL,
        "status": NEEDS_SOURCE,
        "note": "PDF-календарь 2025 неполон; реальные события из него не реконструируются.",
    },
)


def _observation_id(metric: str, period_from: str | None, period_to: str | None, source_id: str) -> str:
    raw = "|".join([metric, period_from or "", period_to or "", source_id])
    return "obs_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def published_observations() -> tuple[Observation, ...]:
    """Every aggregate a published post stated, with its provenance."""

    out: list[Observation] = []
    for item in history.observations():
        out.append(
            Observation(
                observation_id=_observation_id(item.metric, item.period_from, item.period_to, item.source_id),
                metric=item.metric,
                period_from=item.period_from,
                period_to=item.period_to,
                grain=item.grain,
                value=item.value,
                unit=item.unit,
                source_id=item.source_id,
                provenance="published",
                coverage=history.COMPLETE,
                status=OK,
                note=f"публикация {item.source_url}",
            )
        )
    return tuple(out)


def candidate_observations() -> tuple[Observation, ...]:
    """The documented non-facts: visible, unresolved, never summed."""

    out: list[Observation] = []
    for item in _UNRESOLVED:
        out.append(
            Observation(
                observation_id=_observation_id(item["metric"], item["period_from"], item["period_to"], item["source_id"]),
                **item,
            )
        )
    return tuple(out)


def ingest(conn: sqlite3.Connection, *, now: str | None = None) -> dict[str, int]:
    """Write both layers into ``historical_observations`` (idempotent)."""

    stamp = now or _utcnow()
    written = 0
    for observation in (*published_observations(), *candidate_observations()):
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO historical_observations (
                    observation_id, metric, period_from, period_to, grain, value, unit, source_id,
                    provenance, coverage, confidence, status, conflict_group, notes, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (observation_id) DO UPDATE SET
                    value = excluded.value, status = excluded.status, notes = excluded.notes,
                    provenance = excluded.provenance, coverage = excluded.coverage
                """,
                (
                    observation.observation_id,
                    observation.metric,
                    observation.period_from or "",
                    observation.period_to or observation.period_from or "",
                    observation.grain,
                    None if observation.value is None else str(observation.value),
                    observation.unit,
                    observation.source_id,
                    observation.provenance,
                    observation.coverage,
                    None,
                    observation.status,
                    None,
                    observation.note,
                    stamp,
                ),
            )
        written += 1
    return {"written": written}


def _row_to_observation(row: sqlite3.Row) -> Observation:
    raw_value = row["value"]
    return Observation(
        observation_id=str(row["observation_id"]),
        metric=str(row["metric"]),
        period_from=row["period_from"] or None,
        period_to=row["period_to"] or None,
        grain=str(row["grain"]),
        value=None if raw_value in (None, "") else int(raw_value),
        unit=str(row["unit"]),
        source_id=str(row["source_id"]),
        provenance=str(row["provenance"]),
        coverage=str(row["coverage"]),
        status=str(row["status"] or OK),
        note=str(row["notes"] or ""),
    )


def observations(conn: sqlite3.Connection) -> tuple[Observation, ...]:
    rows = conn.execute("SELECT * FROM historical_observations ORDER BY metric, period_from").fetchall()
    return tuple(_row_to_observation(row) for row in rows)


def verified_facts(conn: sqlite3.Connection) -> tuple[Observation, ...]:
    """Only these may be stated as historical facts."""

    return tuple(observation for observation in observations(conn) if observation.promotable)


def unresolved(conn: sqlite3.Connection) -> tuple[Observation, ...]:
    return tuple(observation for observation in observations(conn) if not observation.promotable)


def promote(conn: sqlite3.Connection, observation_id: str, *, actor: str, now: str | None = None) -> Observation:
    """Accept an observation as a published fact — only when it really is one."""

    row = conn.execute("SELECT * FROM historical_observations WHERE observation_id = ?", (observation_id,)).fetchone()
    if row is None:
        raise HistoryError("нет такого наблюдения")
    observation = _row_to_observation(row)
    if not observation.promotable:
        reasons = []
        if observation.provenance != "published":
            reasons.append(f"provenance={observation.provenance}")
        if observation.status != OK:
            reasons.append(f"status={observation.status}")
        if observation.coverage != history.COMPLETE:
            reasons.append(f"coverage={observation.coverage}")
        if observation.value is None:
            reasons.append("значение неизвестно")
        if observation.period_from is None:
            reasons.append("период не подтверждён")
        raise HistoryError("наблюдение нельзя превратить в факт: " + ", ".join(reasons))
    stamp = now or _utcnow()
    with transaction(conn):
        conn.execute(
            "UPDATE historical_observations SET status = ?, ingested_at = ? WHERE observation_id = ?",
            (OK, stamp, observation_id),
        )
    repo.audit(conn, action="history.promoted", actor=actor, entity_type="historical_observation", entity_key=observation_id, now=stamp)
    return observation


# --------------------------------------------------------------------------- #
# conflicts
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Conflict:
    conflict_id: str
    conflict_group: str
    metric: str | None
    period_from: str | None
    period_to: str | None
    published_value: str | None
    computed_value: str | None
    difference: str | None
    status: str
    notes: str | None


def conflicts(conn: sqlite3.Connection, *, status: str | None = None) -> tuple[Conflict, ...]:
    sql = "SELECT * FROM historical_conflicts"
    args: tuple[Any, ...] = ()
    if status is not None:
        sql += " WHERE status = ?"
        args = (status,)
    rows = conn.execute(sql + " ORDER BY conflict_id", args).fetchall()
    return tuple(
        Conflict(
            conflict_id=str(row["conflict_id"]),
            conflict_group=str(row["conflict_group"]),
            metric=row["metric"],
            period_from=row["period_from"],
            period_to=row["period_to"],
            published_value=row["published_value"],
            computed_value=row["computed_value"],
            difference=row["difference"],
            status=str(row["status"]),
            notes=row["notes"],
        )
        for row in rows
    )


def record_conflict(
    conn: sqlite3.Connection,
    *,
    conflict_id: str,
    conflict_group: str,
    metric: str | None,
    period_from: str | None,
    period_to: str | None,
    published_value: Any,
    computed_value: Any,
    notes: str,
    now: str | None = None,
) -> Conflict:
    """A conflict is recorded, never auto-resolved."""

    published = None if published_value is None else str(published_value)
    computed = None if computed_value is None else str(computed_value)
    difference = None
    if published is not None and computed is not None:
        try:
            difference = str(int(published) - int(computed))
        except ValueError:  # pragma: no cover - non-numeric aggregates
            difference = None
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO historical_conflicts (
                conflict_id, conflict_group, metric, period_from, period_to,
                published_value, computed_value, difference, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            ON CONFLICT (conflict_id) DO UPDATE SET
                published_value = excluded.published_value,
                computed_value = excluded.computed_value,
                difference = excluded.difference,
                notes = excluded.notes
            """,
            (conflict_id, conflict_group, metric, period_from, period_to, published, computed, difference, notes),
        )
    return Conflict(conflict_id, conflict_group, metric, period_from, period_to, published, computed, difference, "open", notes)


def close_conflict(conn: sqlite3.Connection, conflict_id: str, *, actor: str, note: str, now: str | None = None) -> bool:
    stamp = now or _utcnow()
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE historical_conflicts SET status = 'closed', notes = COALESCE(?, notes) WHERE conflict_id = ? AND status != 'closed'",
            (note or None, conflict_id),
        )
    if cursor.rowcount:
        repo.audit(conn, action="history.conflict.closed", actor=actor, entity_type="historical_conflict", entity_key=conflict_id, detail={"note": note}, now=stamp)
    return bool(cursor.rowcount)


def detect_conflicts(conn: sqlite3.Connection, *, now: str | None = None) -> tuple[Conflict, ...]:
    """Record the two documented overlaps and the arithmetic impossibility rule."""

    found: list[Conflict] = []
    facts = published_observations()
    by_key: dict[tuple[str, str, str], Observation] = {}
    for observation in facts:
        if observation.period_from is None or observation.period_to is None:
            continue
        key = (observation.metric, observation.period_from, observation.period_to)
        if key in by_key and by_key[key].value != observation.value:
            other = by_key[key]
            found.append(
                record_conflict(
                    conn,
                    conflict_id=f"conf_{_observation_id(*key, 'same-period')}",
                    conflict_group=f"{observation.metric}:{observation.period_from}..{observation.period_to}",
                    metric=observation.metric,
                    period_from=observation.period_from,
                    period_to=observation.period_to,
                    published_value=other.value,
                    computed_value=observation.value,
                    notes="Один и тот же период опубликован дважды с разными значениями; конфликт остаётся видимым.",
                    now=now,
                )
            )
        by_key[key] = observation

    # a part cannot be larger than the whole that contains it
    for outer in facts:
        if outer.period_from is None or outer.period_to is None or outer.value is None:
            continue
        for inner in facts:
            if inner is outer or inner.metric != outer.metric or inner.value is None:
                continue
            if inner.period_from is None or inner.period_to is None:
                continue
            if inner.period_from >= outer.period_from and inner.period_to <= outer.period_to and inner.period_from != outer.period_from:
                if inner.value > outer.value:
                    found.append(
                        record_conflict(
                            conn,
                            conflict_id=f"conf_{outer.metric}_{inner.period_from}_in_{outer.period_from}",
                            conflict_group=f"{outer.metric}:containment",
                            metric=outer.metric,
                            period_from=inner.period_from,
                            period_to=inner.period_to,
                            published_value=outer.value,
                            computed_value=inner.value,
                            notes=f"Часть ({inner.period_label}) больше целого ({outer.period_label}).",
                            now=now,
                        )
                    )

    # documented, non-public evidence with overlapping variants
    for candidate in candidate_observations():
        if candidate.status == AMBIGUOUS_YEAR:
            found.append(
                record_conflict(
                    conn,
                    conflict_id=f"conf_{candidate.observation_id}",
                    conflict_group="internal-book:variants",
                    metric=candidate.metric,
                    period_from=None,
                    period_to=None,
                    published_value=None,
                    computed_value=None,
                    notes=candidate.note,
                    now=now,
                )
            )
    return tuple(found)


def gaps(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Months inside a year that has an annual total but no published monthly report."""

    out: list[dict[str, Any]] = []
    yearly = [o for o in published_observations() if o.grain == history.YEAR]
    for year_report in yearly:
        year = (year_report.period_from or "")[:4]
        if not year:
            continue
        covered: set[str] = set()
        for observation in published_observations():
            if observation.grain == history.MONTH and observation.period_from:
                covered.add(observation.period_from)
            elif observation.grain == history.RANGE and observation.period_from and observation.period_to:
                start, end = observation.period_from, observation.period_to
                if start[:4] == year and end[:4] == year:
                    index = int(start[5:7])
                    while index <= int(end[5:7]):
                        covered.add(f"{year}-{index:02d}")
                        index += 1
        missing = [f"{year}-{month:02d}" for month in range(1, 13) if f"{year}-{month:02d}" not in covered]
        if missing:
            out.append(
                {
                    "metric": year_report.metric,
                    "year": year,
                    "covered": sorted(covered),
                    "missing": missing,
                    "note": "Месяцы без публикации не восстанавливаются: годовой агрегат не делится пропорционально, "
                    "календарь года остаётся частичным.",
                }
            )
    return tuple(out)
