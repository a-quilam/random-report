"""Pure presentation data for the money page; no ledger or historical fallback."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from ..domain.dto import MoneyDTO


@dataclass(frozen=True)
class CategoryRow:
    label: str
    amount_kopecks: int
    hint: str | None
    share_percent: str | None = None


@dataclass(frozen=True)
class MoneyOwnerData:
    available: bool
    partial_reasons: tuple[str, ...] = ()
    coverage_is_final: bool | None = None
    incoming: tuple[CategoryRow, ...] = ()
    outgoing: tuple[CategoryRow, ...] = ()
    incoming_sum_kopecks: int | None = None
    outgoing_sum_kopecks: int | None = None
    expense_kopecks: int | None = None
    compensation_kopecks: int | None = None
    uncovered_kopecks: int | None = None
    shortfall_kopecks: int | None = None
    excess_kopecks: int | None = None
    result_before_subsidy_kopecks: int | None = None
    result_after_subsidy_kopecks: int | None = None
    subsidy_kopecks: int | None = None
    balance_kopecks: int | None = None
    unclassified_in_kopecks: int | None = None
    unclassified_out_kopecks: int | None = None
    coverage_percent: Decimal | None = None
    balance_as_of: date | None = None

    @property
    def partial(self) -> bool:
        return bool(self.partial_reasons)


def _partial_reasons(money: MoneyDTO, expense: int | None) -> tuple[str, ...]:
    metrics = money.metrics
    labels = metrics.get("status_labels", "")
    public_reasons = {"no_data", "contour_unconfirmed", "period_incomplete", "subsidy_unknown",
                      "unclassified_present", "quarantine_present", "orphan_transfer_leg_present",
                      "pair_amount_mismatch_present"}
    reasons = public_reasons.intersection(labels.split(",")) if isinstance(labels, str) else set()
    if not money.period.complete:
        reasons.add("period_incomplete")
    if metrics.get("unclassified_in_kopecks") or metrics.get("unclassified_out_kopecks"):
        reasons.add("unclassified_present")
    if metrics.get("contour_confirmed") is False:
        reasons.add("contour_unconfirmed")
    if expense is None:
        reasons.add("expense_unknown")
    if metrics.get("coverage_is_final") is False and expense is not None and expense > 0:
        reasons.add("coverage_preliminary")
    if money.completeness != "complete":
        reasons.add("snapshot_incomplete")
    return tuple(sorted(reasons))


def _rows(money: MoneyDTO, direction: str) -> tuple[CategoryRow, ...]:
    rows = []
    seen: set[tuple[str, str]] = set()
    for category in money.categories:
        if category.direction != direction or category.bucket in {
            "commune_subsidy", "internal_transfer", "balance_adjustment"
        }:
            continue
        identity = (category.key, category.bucket)
        if identity not in seen:
            share = str(category.share_percent).strip() if category.share_percent is not None else None
            rows.append(CategoryRow(category.label, category.amount_kopecks, category.hint, share or None))
            seen.add(identity)
    return tuple(rows)


def _percent(raw: object) -> Decimal | None:
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def money_owner_data(money: MoneyDTO | None) -> MoneyOwnerData:
    if money is None:
        return MoneyOwnerData(available=False)

    def amount(key: str) -> int | None:
        value = money.metrics.get(key)
        return value if type(value) is int else None

    expense = amount("expense_kopecks")
    compensation = amount("compensation_kopecks")
    raw = money.metrics.get("coverage_percent")
    coverage = _percent(raw) if expense is not None and expense > 0 and compensation is not None else None
    incoming, outgoing = _rows(money, "in"), _rows(money, "out")
    reasons = _partial_reasons(money, expense)

    def row_sum(rows: tuple[CategoryRow, ...]) -> int | None:
        if rows or (money.completeness == "complete" and amount("category_sum_kopecks") is not None):
            return sum(row.amount_kopecks for row in rows)
        return None

    final = money.metrics.get("coverage_is_final")
    balance = amount("balance_kopecks")
    return MoneyOwnerData(
        available=True,
        partial_reasons=reasons,
        coverage_is_final=final if isinstance(final, bool) else None,
        incoming=incoming,
        outgoing=outgoing,
        incoming_sum_kopecks=row_sum(incoming),
        outgoing_sum_kopecks=row_sum(outgoing),
        expense_kopecks=expense,
        compensation_kopecks=compensation,
        uncovered_kopecks=amount("uncovered_kopecks"),
        shortfall_kopecks=amount("shortfall_kopecks"),
        excess_kopecks=amount("excess_kopecks"),
        result_before_subsidy_kopecks=amount("result_before_subsidy_kopecks"),
        result_after_subsidy_kopecks=amount("result_after_subsidy_kopecks"),
        subsidy_kopecks=amount("subsidy_kopecks"),
        balance_kopecks=balance,
        unclassified_in_kopecks=amount("unclassified_in_kopecks"),
        unclassified_out_kopecks=amount("unclassified_out_kopecks"),
        coverage_percent=coverage,
        balance_as_of=money.as_of if balance is not None else None,
    )
