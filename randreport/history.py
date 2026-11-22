"""Historical layer: published aggregates of past periods and their provenance.

The single seed of the historical plane is the ``history_archive`` module: a
small transcription of the *published* reports of the public Telegram channel,
each one tied to the post it came from. That is the only data this module is
allowed to publish for the past — no prototype numbers, no internal books, no
month that was never reported, no transaction reconstructed from an aggregate.

Key rules (docs/REQUIREMENTS.md sections 3.7, 12; acceptance H01):

* A historical aggregate is an observation, not a transaction and not an event.
* Addition happens only over a *complete, non-overlapping* coverage of whole
  periods; a period that only falls inside a wider published report is marked
  ``partial`` and the wider report is named as its source, instead of splitting
  it proportionally.
* A paraphrased report is not a source of exact figures: only the numbers the
  original publication actually stated are observations. The original text is
  kept as a link plus a provenance note, never re-typed from a retelling.
* There is no historical balance: the year figure is a difference of flows, and
  the archive refuses to turn it into a cash remainder.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

try:  # the module lives at the repository root, next to the package
    import history_archive
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    raise ModuleNotFoundError(
        "the historical layer needs history_archive.py importable from the repository root"
    ) from exc

from .domain.timeframes import (
    ALL_TIME,
    MONTH,
    RANGE,
    YEAR,
    Period,
    month_bounds,
)

CHANNEL_USERNAME = "random_culture"
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"

OBSERVED_METRICS = ("events", "receipts", "expenses")

COMPLETE = "complete"
PARTIAL = "partial"
UNKNOWN = "unknown"

# Free-text, safety-first wording reused by the pages (kept in one place so the
# public copy cannot drift away from the numbers it describes).
NO_BALANCE_NOTE = (
    "Историческая разница между поступлениями и расходами — это не остаток на счетах "
    "и не фактический взнос коммунаров."
)
PARAPHRASE_NOTE = (
    "Оригинальный текст отчёта не сохранён дословно: пересказ округляет суммы и потому "
    "не является источником точных цифр. Сохранены ссылка на публикацию и структурированный снимок."
)


@dataclass(frozen=True)
class HistoricalReport:
    """One published report, exactly as the public post stated it."""

    post: int
    start: str
    end: str
    published: str
    events: int
    receipts: int | None = None  # whole rubles, only when the post stated them
    expenses: int | None = None

    @property
    def url(self) -> str:
        return f"{CHANNEL_URL}/{self.post}"

    @property
    def grain(self) -> str:
        if self.end == self.start:
            return MONTH
        start_year, start_month = self.start.split("-")
        end_year, end_month = self.end.split("-")
        if start_month == "01" and end_month == "12" and start_year == end_year:
            return YEAR
        return RANGE

    @property
    def label(self) -> str:
        return self.start if self.start == self.end else f"{self.start}..{self.end}"

    @property
    def has_money(self) -> bool:
        return self.receipts is not None and self.expenses is not None


@dataclass(frozen=True)
class HistoricalObservation:
    """A safe, publishable historical fact (mirrors the ``historical_observations`` row)."""

    metric: str
    period_from: str
    period_to: str
    grain: str
    value: int | None
    unit: str
    source_id: str
    source_url: str
    provenance: str
    coverage: str

    @property
    def period_label(self) -> str:
        return self.period_from if self.period_from == self.period_to else f"{self.period_from}..{self.period_to}"


@dataclass(frozen=True)
class MetricView:
    """What can be said about one metric of one requested period."""

    metric: str
    value: int | None
    unit: str
    coverage: str            # complete | partial | unknown
    period_known: bool       # a published report covers the whole period
    source: HistoricalReport | None
    detail: str


def published_reports() -> tuple[HistoricalReport, ...]:
    """All transcribed public reports, oldest first."""

    return tuple(sorted((HistoricalReport(**vars(r)) for r in history_archive.archive()), key=lambda r: (r.start, r.end)))


def _index(spec: str) -> int:
    day = date.fromisoformat(spec + "-01")
    return 12 * day.year + day.month


def _spec(index: int) -> str:
    year, month = divmod(index - 1, 12)
    return f"{year:04d}-{month + 1:02d}"


def observations() -> tuple[HistoricalObservation, ...]:
    """Flat list of published observations; a metric is only present when stated."""

    out: list[HistoricalObservation] = []
    for report in published_reports():
        source_id = f"tg:{CHANNEL_USERNAME}:{report.post}"
        for metric in OBSERVED_METRICS:
            value = getattr(report, metric)
            if metric == "events":
                value = report.events
            if value is None:
                continue
            out.append(
                HistoricalObservation(
                    metric=metric,
                    period_from=report.start,
                    period_to=report.end,
                    grain=report.grain,
                    value=int(value),
                    unit="events" if metric == "events" else "RUB",
                    source_id=source_id,
                    source_url=report.url,
                    provenance="published",
                    coverage=COMPLETE,
                )
            )
    return tuple(out)


def reports_for_period(period: Period) -> tuple[HistoricalReport, ...]:
    """Published reports overlapping ``period`` (may be wider than the request)."""

    reports = published_reports()
    if period.kind == ALL_TIME:
        return reports
    start, end = period.start, period.end
    if start is None or end is None:  # pragma: no cover - Period guarantees bounds
        raise ValueError("bounded period without bounds")
    first, last = _index(f"{start.year:04d}-{start.month:02d}"), _index(f"{end.year:04d}-{end.month:02d}")
    out = []
    for report in reports:
        r_first, r_last = _index(report.start), _index(report.end)
        if r_first <= last and r_last >= first:
            out.append(report)
    return tuple(out)


def exact_report(period: Period) -> HistoricalReport | None:
    """A single published report that covers exactly this whole period."""

    if period.kind == ALL_TIME:
        return None
    label = f"{period.start.year:04d}-{period.start.month:02d}"
    if period.kind == MONTH:
        for report in published_reports():
            if report.start == label and report.end == label:
                return report
        return None
    end = f"{period.end.year:04d}-{period.end.month:02d}"
    for report in published_reports():
        if report.start == label and report.end == end:
            return report
    return None


def covering_report(period: Period) -> HistoricalReport | None:
    """The narrowest published report that overlaps ``period`` (for a covered month)."""

    reports = reports_for_period(period)
    if not reports:
        return None
    return min(reports, key=lambda r: _index(r.end) - _index(r.start))


def _tiling(metric: str, first: int, last: int) -> int | None:
    """Sum published observations for ``metric`` over ``[first, last]`` exactly.

    A value is returned only for a *complete, non-overlapping* coverage made of
    whole published periods; otherwise ``None``. This mirrors the archive helper
    but is re-stated here so the historical layer never silently sums a gap.
    """

    rows = sorted(
        ((_index(r.start), _index(r.end), getattr(r, metric)) for r in published_reports()),
        key=lambda row: -row[1],
    )
    seen: dict[int, int | None] = {}

    def tile(at: int) -> int | None:
        if at == last + 1:
            return 0
        if at in seen:
            return seen[at]
        result: int | None = None
        for left, right, value in rows:
            if left == at and right <= last and value is not None:
                suffix = tile(right + 1)
                if suffix is not None:
                    result = int(value) + suffix
                    break
        seen[at] = result
        return result

    return tile(first)


def _period_indices(period: Period) -> tuple[int, int]:
    if period.kind == ALL_TIME:
        reports = published_reports()
        return _index(reports[0].start), _index(reports[-1].end)
    start, end = period.start, period.end
    if start is None or end is None:  # pragma: no cover - Period guarantees bounds
        raise ValueError("bounded period without bounds")
    return _index(f"{start.year:04d}-{start.month:02d}"), _index(f"{end.year:04d}-{end.month:02d}")


def metric_view(period: Period, metric: str) -> MetricView:
    """Resolve one metric for one period with an explicit coverage state."""

    if metric not in OBSERVED_METRICS:
        raise ValueError(f"unsupported historical metric {metric!r}")
    if period.kind == ALL_TIME:
        known = published_reports()
        last = known[-1]
        span = Period(kind=RANGE, label=last.label, start=month_bounds(last.start)[0], end=month_bounds(last.end)[1])
    else:
        span = period
    first, last = _period_indices(span)
    value = _tiling(metric, first, last)

    exact = exact_report(period)
    overlapping = reports_for_period(period)
    period_known = exact is not None
    unit = "events" if metric == "events" else "RUB"

    if value is not None:
        source = exact if exact is not None else _covering(period, metric)
        detail = (
            "опубликованный агрегат целого периода"
            if exact is not None
            else "сумма неперекрывающихся опубликованных периодов"
        )
        return MetricView(metric, value, unit, COMPLETE, period_known, source, detail)

    if not overlapping:
        return MetricView(metric, None, unit, UNKNOWN, False, None, "нет опубликованных данных за этот период")

    stated = [r for r in overlapping if getattr(r, metric) is not None]
    if not stated:
        return MetricView(
            metric,
            None,
            unit,
            UNKNOWN,
            period_known,
            covering_report(span),
            "период покрыт публикацией, но эта величина в ней не названа",
        )
    source = covering_report(span)
    return MetricView(
        metric,
        None,
        unit,
        PARTIAL,
        period_known,
        source,
        f"период не делится без остатка: величина названа только внутри отчёта {source.label if source else '—'}",
    )


def _covering(period: Period, metric: str) -> HistoricalReport | None:
    for report in reports_for_period(period):
        if getattr(report, metric) is not None:
            return report
    return None


def available_periods() -> tuple[tuple[str, str, str], ...]:
    """``(start, end, grain)`` of every published report, for the archive index."""

    return tuple((r.start, r.end, r.grain) for r in published_reports())


def msk_today(today: date | None = None) -> date:
    return today or date.today()


def coverage_percent(receipts_kopecks: int | None, expenses_kopecks: int | None) -> Decimal | None:
    """Coverage of expenses by receipts, ``None`` when it is not applicable.

    Historical amounts arrive in whole rubles; the percentage is computed on
    integers, never on floats, and a zero expense base is "not applicable".
    """

    if receipts_kopecks is None or expenses_kopecks is None or expenses_kopecks == 0:
        return None
    return (Decimal(receipts_kopecks) * Decimal(100) / Decimal(expenses_kopecks)).quantize(Decimal("0.1"))


def annual_view(year: int) -> tuple[MetricView, MetricView, MetricView]:
    """The three published metrics of a whole year."""

    period = Period(kind=YEAR, label=str(year), start=date(year, 1, 1), end=date(year, 12, 31))
    return (
        metric_view(period, "events"),
        metric_view(period, "receipts"),
        metric_view(period, "expenses"),
    )


def years() -> tuple[int, ...]:
    return tuple(sorted({int(r.start[:4]) for r in published_reports()} | {int(r.end[:4]) for r in published_reports()}))


def month_labels() -> tuple[str, ...]:
    """Every calendar month touched by a published report (whole-month period labels)."""

    out: set[str] = set()
    for report in published_reports():
        first, last = _index(report.start), _index(report.end)
        for index in range(first, last + 1):
            out.add(_spec(index))
    return tuple(sorted(out))
