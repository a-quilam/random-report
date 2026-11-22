"""Economic classes of money flows and account scope.

The internal enum is stable; public category labels never depend on the letter
case of historical source names (docs/REQUIREMENTS.md section 6.3).
"""

from __future__ import annotations

from enum import StrEnum


class EconomicClass(StrEnum):
    EXTERNAL_COMPENSATION = "external_compensation"
    COMMUNE_SUBSIDY = "commune_subsidy"
    OPERATING_EXPENSE = "operating_expense"
    EXPENSE_REFUND = "expense_refund"
    COMPENSATION_REFUND = "compensation_refund"
    FINANCING_IN = "financing_in"
    FINANCING_OUT = "financing_out"
    INTERNAL_TRANSFER = "internal_transfer"
    BALANCE_ADJUSTMENT = "balance_adjustment"
    UNCLASSIFIED = "unclassified"


class AccountScope(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNKNOWN = "unknown"


OPERATING_CLASSES = frozenset(
    {
        EconomicClass.EXTERNAL_COMPENSATION,
        EconomicClass.COMMUNE_SUBSIDY,
        EconomicClass.OPERATING_EXPENSE,
        EconomicClass.EXPENSE_REFUND,
        EconomicClass.COMPENSATION_REFUND,
    }
)

NON_OPERATING_CLASSES = frozenset(
    {
        EconomicClass.FINANCING_IN,
        EconomicClass.FINANCING_OUT,
        EconomicClass.INTERNAL_TRANSFER,
        EconomicClass.BALANCE_ADJUSTMENT,
    }
)

DIRECTION_BY_CLASS = {
    EconomicClass.EXTERNAL_COMPENSATION: "in",
    EconomicClass.COMMUNE_SUBSIDY: "in",
    EconomicClass.OPERATING_EXPENSE: "out",
    EconomicClass.EXPENSE_REFUND: "in",
    EconomicClass.COMPENSATION_REFUND: "out",
    EconomicClass.FINANCING_IN: "in",
    EconomicClass.FINANCING_OUT: "out",
    EconomicClass.INTERNAL_TRANSFER: "internal",
    EconomicClass.BALANCE_ADJUSTMENT: "internal",
    # UNCLASSIFIED keeps the direction of the movement itself: an unknown category
    # must stay visible on the side it actually moved
}


def is_operating(economic_class: EconomicClass) -> bool:
    return economic_class in OPERATING_CLASSES


def direction_for(economic_class: EconomicClass, fallback: str = "unknown") -> str:
    return DIRECTION_BY_CLASS.get(economic_class, fallback)
