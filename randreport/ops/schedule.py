"""Scheduling of the single daily cycle at 03:00 Europe/Moscow.

The schedule is expressed in the Moscow timezone explicitly and never in the host
timezone. Catch-up recovers a missed daily cycle; it is not a new polling
cadence, and it never turns the nightly job into an hourly one
(docs/REQUIREMENTS.md section 9).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from ..domain.timeframes import MSK

RUN_HOUR_MSK = 3

# Verified with `systemd-analyze calendar` on Debian before installation; the
# unit files themselves are only templates until a server is chosen.
SYSTEMD_CALENDAR = f"*-*-* {RUN_HOUR_MSK:02d}:00:00 Europe/Moscow"

SYSTEMD_TIMER_UNIT = """# Template only: do not install before a server is chosen (docs/RUNBOOK.md).
[Unit]
Description=RanDom nightly report sync (03:00 Europe/Moscow)

[Timer]
OnCalendar={calendar}
Persistent=true
Unit=randreport-nightly.service

[Install]
WantedBy=timers.target
""".format(calendar=SYSTEMD_CALENDAR)

MAX_CATCH_UP_DAYS = 7


def logical_date_for(instant: datetime) -> str:
    """The MSK calendar day a run belongs to (the idempotency key date)."""

    if instant.tzinfo is None:
        raise ValueError("instant must be timezone aware")
    return instant.astimezone(MSK).date().isoformat()


def scheduled_instant(logical_date: str) -> datetime:
    day = date.fromisoformat(logical_date)
    return datetime.combine(day, time(RUN_HOUR_MSK, 0), tzinfo=MSK)


def next_run_at(now: datetime) -> datetime:
    """The next 03:00 MSK strictly after ``now`` (host timezone is irrelevant)."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    local = now.astimezone(MSK)
    candidate = datetime.combine(local.date(), time(RUN_HOUR_MSK, 0), tzinfo=MSK)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def is_due(now: datetime, last_success_logical_date: str | None) -> bool:
    """True when the current MSK day has not had a successful run yet and is past 03:00."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    local = now.astimezone(MSK)
    if local.time() < time(RUN_HOUR_MSK, 0):
        return False
    return last_success_logical_date != local.date().isoformat()


def missed_logical_dates(
    last_success_logical_date: str | None,
    now: datetime,
    *,
    max_days: int = MAX_CATCH_UP_DAYS,
) -> tuple[str, ...]:
    """Days between the last success and today that have no successful run."""

    today = now.astimezone(MSK).date()
    if last_success_logical_date is None:
        return (today.isoformat(),)
    start = date.fromisoformat(last_success_logical_date) + timedelta(days=1)
    days: list[str] = []
    cursor = start
    while cursor < today and len(days) < max_days:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(days)


def systemd_timer_text() -> str:
    return SYSTEMD_TIMER_UNIT


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_days(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(str(value) for value in values))
