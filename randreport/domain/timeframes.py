"""Accounting timeframes in Europe/Moscow.

Periods are whole calendar months, whole years, ranges of whole months or all
time. Sub-month "buckets" are never a public period: they isolate single
operations and single closed events (docs/REQUIREMENTS.md sections 6.6, 7).

Fact dates use the account timezone Europe/Moscow, never the host timezone and
never a "last edited" timestamp.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
YEAR_RE = re.compile(r"^(\d{4})$")

MONTH = "month"
YEAR = "year"
RANGE = "range"
ALL_TIME = "all_time"

WHOLE_KINDS = (MONTH, YEAR, RANGE)


class PeriodError(ValueError):
    """A period that cannot exist."""


class UnsafePeriodError(PeriodError):
    """A period that must not be published (too narrow / not aligned to a whole month)."""


@dataclass(frozen=True)
class Period:
    kind: str
    label: str
    start: date | None = None
    end: date | None = None

    def __post_init__(self) -> None:
        if self.kind == ALL_TIME:
            if self.start is not None or self.end is not None:
                raise PeriodError("all_time must not carry bounds")
            return
        if self.kind not in WHOLE_KINDS:
            raise PeriodError(f"unknown period kind {self.kind!r}")
        if self.start is None or self.end is None:
            raise PeriodError("bounded period requires start and end")
        if self.start > self.end:
            raise PeriodError("period start is after its end")
        if self.start.day != 1 or self.end != last_day_of_month(self.end):
            raise UnsafePeriodError("public periods must cover whole calendar months")

    def contains(self, day: date) -> bool:
        if self.kind == ALL_TIME:
            return True
        assert self.start is not None and self.end is not None
        return self.start <= day <= self.end

    def months(self) -> tuple[str, ...]:
        if self.kind == ALL_TIME:
            return ()
        assert self.start is not None and self.end is not None
        specs: list[str] = []
        cursor = self.start
        while cursor <= self.end:
            specs.append(f"{cursor.year:04d}-{cursor.month:02d}")
            cursor = first_day_of_next_month(cursor)
        return tuple(specs)

    def is_complete(self, as_of: date) -> bool:
        """True when the whole period is already in the past (fully elapsed)."""

        if self.kind == ALL_TIME:
            return True
        assert self.end is not None
        return self.end <= as_of

    def bucket(self) -> str:
        if self.kind == ALL_TIME:
            return ALL_TIME
        assert self.start is not None and self.end is not None
        if self.kind == MONTH:
            return f"month:{self.start.year:04d}-{self.start.month:02d}"
        if self.kind == YEAR:
            return f"year:{self.start.year:04d}"
        return f"range:{self.start.year:04d}-{self.start.month:02d}..{self.end.year:04d}-{self.end.month:02d}"


def last_day_of_month(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def first_day_of_next_month(day: date) -> date:
    return last_day_of_month(day) + timedelta(days=1)


def month_bounds(spec: str) -> tuple[date, date]:
    match = MONTH_RE.match(spec)
    if not match:
        raise UnsafePeriodError(f"not a whole month: {spec!r}")
    year, month = int(match.group(1)), int(match.group(2))
    start = date(year, month, 1)
    return start, last_day_of_month(start)


def month_period(spec: str) -> Period:
    start, end = month_bounds(spec)
    return Period(kind=MONTH, label=spec, start=start, end=end)


def year_period(spec: str) -> Period:
    match = YEAR_RE.match(spec)
    if not match:
        raise UnsafePeriodError(f"not a whole year: {spec!r}")
    year = int(match.group(1))
    return Period(kind=YEAR, label=spec, start=date(year, 1, 1), end=date(year, 12, 31))


def range_period(start_spec: str, end_spec: str) -> Period:
    start, _ = month_bounds(start_spec)
    _, end = month_bounds(end_spec)
    if start > end:
        raise PeriodError("range start is after range end")
    return Period(kind=RANGE, label=f"{start_spec}..{end_spec}", start=start, end=end)


def parse_period(spec: str) -> Period:
    """Resolve a public period token: ``YYYY-MM``, ``YYYY`` or ``all``."""

    text = (spec or "").strip()
    if text == "all":
        return Period(kind=ALL_TIME, label=ALL_TIME, start=None, end=None)
    if MONTH_RE.match(text):
        return month_period(text)
    if YEAR_RE.match(text):
        return year_period(text)
    raise UnsafePeriodError(f"unsupported public period {spec!r}")


def msk_day(moment: datetime) -> date:
    """Fact date of an instant in Europe/Moscow."""

    if moment.tzinfo is None:
        raise PeriodError("naive datetimes are not accepted; attach a timezone")
    return moment.astimezone(MSK).date()


def day_bounds_msk(day: date) -> tuple[datetime, datetime]:
    """UTC instants of the Moscow day ``[day 00:00, day+1 00:00)``."""

    start = datetime.combine(day, time(0, 0), tzinfo=MSK)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=MSK)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def previous_month(spec: str) -> str:
    start, _ = month_bounds(spec)
    cursor = start - timedelta(days=1)
    return f"{cursor.year:04d}-{cursor.month:02d}"


def same_month_previous_year(spec: str) -> str:
    start, _ = month_bounds(spec)
    return f"{start.year - 1:04d}-{start.month:02d}"
