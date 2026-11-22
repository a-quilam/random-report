"""Per-source reliability state: last attempt, last success, freshness, coverage.

Freshness and verification are independent attributes: a value verified
yesterday is not "fresh" today after a failed cycle. A failed source keeps its
last validated projection, explicitly marked stale, and is never replaced by a
zero (docs/REQUIREMENTS.md section 9).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from ..store import repository as repo

STALE_AFTER_HOURS = 26.0


@dataclass(frozen=True)
class SourceStatus:
    source: str
    status: str
    last_attempt_at: str | None
    last_success_at: str | None
    data_as_of: str | None
    coverage: str
    error_count: int
    last_error: str | None
    has_last_good: bool
    freshness: str = "unknown"


def freshness_for(last_success_at: str | None, *, now: datetime | None = None, stale_after_hours: float = STALE_AFTER_HOURS) -> str:
    if not last_success_at:
        return "unknown"
    moment = now or datetime.now(timezone.utc)
    try:
        succeeded = datetime.fromisoformat(last_success_at)
    except ValueError:
        return "unknown"
    if succeeded.tzinfo is None:
        succeeded = succeeded.replace(tzinfo=timezone.utc)
    age_hours = (moment - succeeded).total_seconds() / 3600.0
    return "fresh" if age_hours <= stale_after_hours else "stale"


def source_status(
    conn: sqlite3.Connection,
    source: str,
    *,
    now: datetime | None = None,
    stale_after_hours: float = STALE_AFTER_HOURS,
) -> SourceStatus:
    rows = [row for row in repo.source_runs(conn, source)]
    last = rows[-1] if rows else None
    successes = [row for row in rows if row.get("last_success_at")]
    last_success = max((row["last_success_at"] for row in successes), default=None)
    good = repo.last_good(conn, source)
    return SourceStatus(
        source=source,
        status=(last or {}).get("status", "unknown"),
        last_attempt_at=(last or {}).get("last_attempt_at"),
        last_success_at=last_success,
        data_as_of=(good or {}).get("data_as_of") or (last or {}).get("data_as_of"),
        coverage=(good or {}).get("coverage") or (last or {}).get("coverage") or "unknown",
        error_count=int((last or {}).get("error_count") or 0),
        last_error=(last or {}).get("error"),
        has_last_good=good is not None,
        freshness=freshness_for(last_success, now=now, stale_after_hours=stale_after_hours),
    )


def overall_freshness(statuses: Iterable[SourceStatus]) -> str:
    """A release is fresh only when every source is fresh and none is missing."""

    seen = list(statuses)
    if not seen:
        return "unknown"
    if any(status.freshness != "fresh" for status in seen):
        return "stale"
    return "fresh"


def section_freshness(statuses: Mapping[str, SourceStatus], sources: Iterable[str]) -> str:
    states = [statuses[name].freshness for name in sources if name in statuses]
    if not states:
        return "unknown"
    return "fresh" if all(state == "fresh" for state in states) else "stale"


def coverage_for(statuses: Mapping[str, SourceStatus], sources: Iterable[str]) -> str:
    coverages = [statuses[name].coverage for name in sources if name in statuses]
    if not coverages:
        return "unknown"
    if any(coverage != "complete" for coverage in coverages):
        return "partial"
    return "complete"
