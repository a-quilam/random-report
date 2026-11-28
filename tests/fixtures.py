"""Synthetic fixtures for the public-contour domain tests.

Everything in this module is invented for tests: no real accounts, tokens,
calendars, posts or production payloads. All money amounts are integer kopecks.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from randreport.domain.events import UNVERSIONED_REVISION, EventType, EventTypeMapping, Occurrence
from randreport.domain.finance import Movement, ScopePolicy
from randreport.domain.flows import EconomicClass
from randreport.domain.timeframes import MSK, msk_day

PUBLIC_ACCOUNTS = ("acc_cash", "acc_card", "acc_savings")
PRIVATE_ACCOUNTS = ("acc_personal",)
QUARANTINED_ACCOUNTS = ("acc_mixed",)

CATEGORY_CLASSES = {
    "cat_rent": EconomicClass.OPERATING_EXPENSE,
    "cat_utilities": EconomicClass.OPERATING_EXPENSE,
    "cat_equipment": EconomicClass.OPERATING_EXPENSE,
    "cat_supplies": EconomicClass.OPERATING_EXPENSE,
    "cat_event_costs": EconomicClass.OPERATING_EXPENSE,
    "cat_commission": EconomicClass.OPERATING_EXPENSE,
    "cat_donations": EconomicClass.EXTERNAL_COMPENSATION,
    "cat_event_share": EconomicClass.EXTERNAL_COMPENSATION,
    "cat_warehouse": EconomicClass.EXTERNAL_COMPENSATION,
    "cat_bottles": EconomicClass.EXTERNAL_COMPENSATION,
    "cat_purchase_refund": EconomicClass.EXPENSE_REFUND,
    "cat_donation_refund": EconomicClass.COMPENSATION_REFUND,
    "cat_commune_subsidy": EconomicClass.COMMUNE_SUBSIDY,
    "cat_commune_direct_pay": EconomicClass.COMMUNE_SUBSIDY,
    "cat_loan_in": EconomicClass.FINANCING_IN,
    "cat_loan_repay": EconomicClass.FINANCING_OUT,
    "cat_balance_adjustment": EconomicClass.BALANCE_ADJUSTMENT,
}

CATEGORY_LABELS = {
    "cat_rent": "Аренда",
    "cat_utilities": "Коммунальные",
    "cat_equipment": "Оборудование",
    "cat_supplies": "Расходники",
    "cat_event_costs": "Расходы на события",
    "cat_commission": "Комиссии",
    "cat_donations": "Донаты",
    "cat_event_share": "Доля с ивента",
    "cat_warehouse": "Склад",
    "cat_bottles": "Напитки",
    "cat_purchase_refund": "Возврат покупок",
    "cat_donation_refund": "Возвраты донатов",
    "cat_commune_subsidy": "Взносы коммунаров",
    "cat_commune_direct_pay": "Прямая оплата коммунаром",
    "cat_loan_in": "Займы полученные",
    "cat_loan_repay": "Погашение займов",
    "cat_balance_adjustment": "Корректировка баланса",
}

SUBSIDY_CATEGORIES = frozenset({"cat_commune_subsidy", "cat_commune_direct_pay"})
QUARANTINED_CATEGORIES = frozenset({"cat_flat_shared"})  # mixed living/public purpose
COMMISSION_CATEGORIES = frozenset({"cat_commission"})

SUBCALENDARS = {
    "sub_open": EventType.OPEN,
    "sub_registration": EventType.OPEN_BY_REGISTRATION,
    "sub_closed": EventType.CLOSED,
    "sub_internal": EventType.INTERNAL_MEETING,
    "sub_preparation": EventType.PREPARATION,
    "sub_lecture": EventType.UNKNOWN,
    # placeholder IDs for the closed-contour roles (no real Teamup values)
    "sub_meetings": EventType.CLOSED,
    "sub_rent": EventType.CLOSED,
    "sub_excluded": EventType.CLOSED,
}
PRIVATE_SUBCALENDARS = frozenset({"sub_closed", "sub_internal", "sub_meetings", "sub_rent", "sub_excluded"})
# The allowed closed contour: only here an exact «Собрания» title becomes a meeting.
MEETING_SUBCALENDARS = frozenset({"sub_meetings"})
# Closed rentals by mapping — not every closed record is a rent.
RENT_SUBCALENDARS = frozenset({"sub_rent"})
# An excluded subcalendar yields neither lists nor totals.
EXCLUDED_SUBCALENDARS = frozenset({"sub_excluded"})


def policy(**overrides) -> ScopePolicy:
    base = dict(
        version="policy-synthetic-1",
        public_accounts=frozenset(PUBLIC_ACCOUNTS),
        private_accounts=frozenset(PRIVATE_ACCOUNTS),
        quarantined_accounts=frozenset(QUARANTINED_ACCOUNTS),
        category_classes=dict(CATEGORY_CLASSES),
        category_labels=dict(CATEGORY_LABELS),
        quarantined_categories=QUARANTINED_CATEGORIES,
        commission_categories=COMMISSION_CATEGORIES,
        subsidy_category_ids=SUBSIDY_CATEGORIES,
        count_visibility_threshold_kopecks=100000,
    )
    base.update(overrides)
    return ScopePolicy(**base)


def as_day(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def movement(
    movement_id: str,
    on: str | date,
    amount_kopecks: int,
    *,
    source: str | None = None,
    target: str | None = None,
    category: str | None = None,
    currency: str = "RUB",
    pair_id: str | None = None,
    pair_kind: str | None = None,
    reimbursement_of: str | None = None,
    is_planned: bool = False,
    deleted: bool = False,
    revision: int = 1,
) -> Movement:
    return Movement(
        id=movement_id,
        occurred_on=as_day(on),
        amount_kopecks=amount_kopecks,
        source_account=source,
        target_account=target,
        category_id=category,
        currency=currency,
        pair_id=pair_id,
        pair_kind=pair_kind,
        reimbursement_of=reimbursement_of,
        is_planned=is_planned,
        deleted=deleted,
        source_revision=revision,
    )


def expense(movement_id: str, on, amount_kopecks: int, category: str, account: str = "acc_card", **kw) -> Movement:
    return movement(movement_id, on, amount_kopecks, source=account, category=category, **kw)


def income(movement_id: str, on, amount_kopecks: int, category: str, account: str = "acc_card", **kw) -> Movement:
    return movement(movement_id, on, amount_kopecks, target=account, category=category, **kw)


def split_evenly(total_kopecks: int, parts: int = 12) -> list[int]:
    """Split an integer total into `parts` positive integer chunks summing exactly."""
    if parts <= 0 or total_kopecks < parts:
        raise ValueError("cannot split")
    base, remainder = divmod(total_kopecks, parts)
    chunks = [base] * (parts - 1) + [base + remainder]
    assert sum(chunks) == total_kopecks
    return chunks


def monthly_movements(
    category: str,
    total_kopecks: int,
    year: int,
    *,
    prefix: str,
    direction: str = "in",
    account: str = "acc_card",
) -> list[Movement]:
    out: list[Movement] = []
    for index, chunk in enumerate(split_evenly(total_kopecks, 12), start=1):
        day = date(year, index, 5)
        builder = income if direction == "in" else expense
        out.append(builder(f"{prefix}_{index:02d}", day, chunk, category, account=account))
    return out


def msk(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def event_mapping(**overrides) -> EventTypeMapping:
    base = dict(
        version="event-mapping-synthetic-1",
        subcalendar_types=dict(SUBCALENDARS),
        private_subcalendars=PRIVATE_SUBCALENDARS,
        excluded_subcalendars=EXCLUDED_SUBCALENDARS,
        meeting_subcalendars=MEETING_SUBCALENDARS,
        rent_subcalendars=RENT_SUBCALENDARS,
    )
    base.update(overrides)
    return EventTypeMapping(**base)


def occurrence(
    occurrence_id: str,
    start: datetime,
    end: datetime | None,
    *,
    subcalendars: Sequence[str] = ("sub_open",),
    calendar: str = "cal_main",
    title: str | None = None,
    series_id: str | None = None,
    all_day: bool = False,
    raw_type: str | None = None,
    revision: str = UNVERSIONED_REVISION,
    deleted: bool = False,
    source_present: bool = True,
    local_archive: bool = False,
) -> Occurrence:
    return Occurrence(
        calendar_id=calendar,
        occurrence_id=occurrence_id,
        start=start,
        end=end,
        subcalendar_ids=tuple(subcalendars),
        series_id=series_id,
        title=title,
        all_day=all_day,
        raw_type=raw_type,
        revision=revision,
        deleted=deleted,
        source_present=source_present,
        local_archive=local_archive,
    )
