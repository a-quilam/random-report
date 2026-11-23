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
    AllocationAdjustmentDTO,
    AllocationDTO,
    CategoryDTO,
    EventTypeAggregateDTO,
    EventsDTO,
    MoneyDTO,
    PeriodDTO,
    PublicEventDTO,
    PublicMeetingDTO,
    RentMonthDTO,
)
from ..domain.allocation import AllocationReport
from ..domain.events import (
    PUBLIC_TYPES,
    EventSelection,
    EventStats,
    event_stats,
    public_calendar_days,
    public_events,
    public_meeting_days,
    public_meetings,
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


def money_metrics(
    totals: FinanceTotals,
    balance: BalanceSummary | None = None,
    allocation: "AllocationReport | None" = None,
) -> dict[str, Any]:
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
        # the contour flag is derived from the ledger, not guessed: an unconfirmed
        # map is the only thing that raises the `contour_unconfirmed` label
        "contour_confirmed": "contour_unconfirmed" not in totals.labels,
    }
    if allocation is not None:
        # the allocation facts travel inside the same metric map, so the JSON,
        # the CSV and the HTML read one source of truth (docs/ALLOCATION.md §6)
        metrics.update(
            {
                "allocation_applied": allocation.applied,
                "allocation_excluded_categories": len(allocation.excluded_categories),
                "allocation_excluded_operations": allocation.excluded_operations,
                "shared_expenses_before_kopecks": allocation.shared_expenses_before_kopecks,
                "shared_expenses_after_kopecks": allocation.shared_expenses_after_kopecks,
                "rent_computed_kopecks": allocation.computed_rent_kopecks,
                "rent_bank_kopecks": allocation.bank_rent_kopecks,
                "rent_applied": allocation.rent_applied,
                "rent_differs": allocation.rent_differs,
                "corrections_positive_kopecks": allocation.corrections_positive_kopecks,
                "corrections_negative_kopecks": allocation.corrections_negative_kopecks,
                "corrections_unclassified": allocation.corrections_unclassified,
                "allocation_labels": ",".join(allocation.labels),
            }
        )
        extra = [label for label in allocation.labels if label not in metrics["status_labels"].split(",")]
        if extra:
            metrics["status_labels"] = ",".join(filter(None, [metrics["status_labels"], *extra]))
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


def allocation_dto(report: AllocationReport | None) -> AllocationDTO:
    """The public explanation of the owner overrides (counts, totals, labels)."""

    if report is None:
        return AllocationDTO()
    public = report.to_public()
    return AllocationDTO(
        version=public["version"],
        confirmed=public["confirmed"],
        applied=public["applied"],
        excluded_categories=public["excluded_categories"],
        excluded_operations=public["excluded_operations"],
        shared_expenses_before_kopecks=public["shared_expenses_before_kopecks"],
        shared_expenses_after_kopecks=public["shared_expenses_after_kopecks"],
        computed_rent_kopecks=public["computed_rent_kopecks"],
        bank_rent_kopecks=public["bank_rent_kopecks"],
        rent_applied=public["rent_applied"],
        rent_differs=public["rent_differs"],
        corrections_positive_kopecks=public["corrections_positive_kopecks"],
        corrections_negative_kopecks=public["corrections_negative_kopecks"],
        corrections_unclassified=public["corrections_unclassified"],
        adjustments=tuple(
            AllocationAdjustmentDTO(
                kind=item["kind"],
                category_key=item["category_key"],
                period=item["period"],
                before_kopecks=item["before_kopecks"],
                after_kopecks=item["after_kopecks"],
                note=item["note"],
            )
            for item in public["adjustments"]
        ),
        rent_months=tuple(
            RentMonthDTO(
                month=item["month"],
                computed_kopecks=item["computed_kopecks"],
                bank_kopecks=item["bank_kopecks"],
                applied=item["applied"],
                note=item["note"],
            )
            for item in public["rent_months"]
        ),
        labels=tuple(public["labels"]),
    )


def build_money_payload(
    ctx: ReleaseContext,
    totals: FinanceTotals,
    *,
    balance: BalanceSummary | None = None,
    params: Mapping[str, str] | None = None,
    allocation: AllocationReport | None = None,
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
            hint=row.hint,
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
        metrics=money_metrics(totals, balance, allocation),
        categories=categories,
        allocation=allocation_dto(allocation),
    )


def events_metrics(stats: EventStats) -> dict[str, Any]:
    return {
        "total_events": stats.total,
        "public_events": stats.public_total,
        "closed_aggregate": stats.closed_total,
        "closed_rent_aggregate": stats.closed_rent_total,
        "meeting_aggregate": stats.meeting_total,
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
    meeting_days = public_meeting_days(selection) if granularity == "day" else {}
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
        meetings=tuple(
            PublicMeetingDTO(on=meeting.on, label=meeting.label) for meeting in public_meetings(selection)
        ),
        event_type_aggregates=dict(stats.by_type),
        event_type_details=details,
        calendar_days=dict(days),
        meeting_days=dict(meeting_days),
        calendar_granularity=granularity,
        metrics=events_metrics(stats),
    )


def release_id_for(payload: Any) -> str:
    """Content-addressed release id; identical data yields an identical release."""

    digest = hashlib.sha256(public_json(payload).encode("utf-8")).hexdigest()
    return f"rel_{digest[:16]}"
