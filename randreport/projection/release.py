"""Build one atomic public release from the domain aggregates.

The builder calls these functions only; the SSR layer renders the returned DTOs
and nothing else. See docs/DOMAIN.md for the frozen contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from ..domain.dto import (
    CategoryDTO,
    EventTypeAggregateDTO,
    EventsDTO,
    MoneyDTO,
    PeriodDTO,
    PublicEventDTO,
)
from ..domain.events import (
    PUBLIC_TYPES,
    EventSelection,
    EventStats,
    event_stats,
    public_calendar_days,
    public_events,
)
from ..domain.finance import BalanceSummary, FinanceTotals, category_public_key
from ..domain.money import format_kopecks
from ..domain.timeframes import Period, msk_day
from .privacy import public_json
from .query import (
    UnsafeQueryError,
    calendar_grid_granularity,
    check_period_allowed,
    resolve_period,
    validate_params,
)

SCHEMA_VERSION = "public-release-1"

MONEY_UNIT = "RUB_kopecks"


@dataclass(frozen=True)
class ReleaseContext:
    """Everything a payload needs besides the computed data."""

    release_id: str
    as_of: datetime
    period: Period
    freshness: str = "fresh"
    verification: str = "operational"
    completeness: str = "complete"
    source_labels: tuple[str, ...] = ()

    def as_of_day(self) -> date:
        return msk_day(self.as_of)

    def period_dto(self) -> PeriodDTO:
        return PeriodDTO(
            requested=self.period.label,
            covered_start=self.period.start,
            covered_end=self.period.end,
            complete=self.period.is_complete(self.as_of_day()),
        )


def _check_params(ctx: ReleaseContext, params: Mapping[str, str] | None) -> None:
    if params is None:
        return
    normalized = validate_params(params)
    period = resolve_period(normalized)
    check_period_allowed(period)
    if period.bucket() != ctx.period.bucket():
        raise UnsafeQueryError("requested period does not match the release period")


def money_metrics(totals: FinanceTotals, balance: BalanceSummary | None = None) -> dict[str, Any]:
    """Public metric names, all money in integer kopecks, percentages as strings."""

    metrics: dict[str, Any] = {
        "gross_expense_kopecks": totals.gross_expense_kopecks,
        "expense_refund_kopecks": totals.expense_refund_kopecks,
        "expense_kopecks": totals.expense_kopecks,
        "external_compensation_kopecks": totals.external_compensation_kopecks,
        "compensation_refund_kopecks": totals.compensation_refund_kopecks,
        "compensation_kopecks": totals.compensation_kopecks,
        "subsidy_kopecks": totals.subsidy_kopecks,
        "uncovered_kopecks": totals.uncovered_kopecks,
        "coverage_percent": None if totals.coverage_percent is None else str(totals.coverage_percent),
        "coverage_is_final": totals.coverage_is_final,
        "shortfall_kopecks": totals.shortfall_kopecks,
        "excess_kopecks": totals.excess_kopecks,
        "result_before_subsidy_kopecks": totals.result_before_subsidy_kopecks,
        "result_after_subsidy_kopecks": totals.result_after_subsidy_kopecks,
        "unclassified_in_kopecks": totals.unclassified_in_kopecks,
        "unclassified_out_kopecks": totals.unclassified_out_kopecks,
        "financing_in_kopecks": totals.financing_in_kopecks,
        "financing_out_kopecks": totals.financing_out_kopecks,
        "balance_adjustment_kopecks": totals.balance_adjustment_kopecks,
        "internal_transfer_kopecks": totals.internal_transfer_kopecks,
        "category_sum_kopecks": totals.category_sum_kopecks,
        "status_labels": ",".join(totals.labels),
    }
    if balance is not None:
        metrics.update(
            {
                "balance_kopecks": balance.kopecks,
                "balance_completeness": balance.completeness,
                "obligations_kopecks": balance.obligations_kopecks,
                "balance_status_labels": ",".join(balance.labels),
            }
        )
    return metrics


def build_money_payload(
    ctx: ReleaseContext,
    totals: FinanceTotals,
    *,
    balance: BalanceSummary | None = None,
    params: Mapping[str, str] | None = None,
) -> MoneyDTO:
    _check_params(ctx, params)
    categories = tuple(
        CategoryDTO(
            key=row.key or category_public_key(row.category_id),
            label=row.label,
            bucket=row.bucket,
            direction=row.direction,
            amount_kopecks=row.amount_kopecks,
            amount_rub=format_kopecks(row.amount_kopecks),
            share_percent=None if row.share_percent is None else str(row.share_percent),
            operations_count=row.operations_count,
        )
        for row in totals.categories
    )
    return MoneyDTO(
        period=ctx.period_dto(),
        as_of=ctx.as_of_day(),
        release_id=ctx.release_id,
        freshness=ctx.freshness,
        verification=ctx.verification,
        completeness=totals.completeness,
        source_labels=tuple(ctx.source_labels),
        unit=MONEY_UNIT,
        metrics=money_metrics(totals, balance),
        categories=categories,
    )


def events_metrics(stats: EventStats) -> dict[str, Any]:
    return {
        "total_events": stats.total,
        "public_events": stats.public_total,
        "closed_aggregate": stats.closed_total,
        "unknown_type_events": stats.by_type["unknown"],
        "local_archive_events": stats.local_archive_total,
        "public_share_percent": None
        if stats.public_share_percent is None
        else str(stats.public_share_percent),
    }


def build_events_payload(
    ctx: ReleaseContext,
    selection: EventSelection,
    *,
    stats: EventStats | None = None,
    params: Mapping[str, str] | None = None,
) -> EventsDTO:
    _check_params(ctx, params)
    stats = stats or event_stats(selection)
    granularity = calendar_grid_granularity(ctx.period)
    days = public_calendar_days(selection) if granularity == "day" else {}
    details = tuple(
        EventTypeAggregateDTO(type=event_type, count=count, public=event_type in PUBLIC_TYPES)
        for event_type, count in sorted(stats.by_type.items())
    )
    return EventsDTO(
        period=ctx.period_dto(),
        as_of=ctx.as_of_day(),
        release_id=ctx.release_id,
        freshness=ctx.freshness,
        verification=ctx.verification,
        completeness=ctx.completeness,
        source_labels=tuple(ctx.source_labels),
        public_events=tuple(
            PublicEventDTO(
                on=event.on,
                type=event.type,
                title=event.title,
                description=event.description,
                announce_url=event.announce_url,
                registration_open=event.registration_open,
            )
            for event in public_events(selection)
        ),
        event_type_aggregates=dict(stats.by_type),
        event_type_details=details,
        calendar_days=dict(days),
        calendar_granularity=granularity,
        metrics=events_metrics(stats),
    )


def release_id_for(payload: Any) -> str:
    """Content-addressed release id; identical data yields an identical release."""

    digest = hashlib.sha256(public_json(payload).encode("utf-8")).hexdigest()
    return f"rel_{digest[:16]}"
