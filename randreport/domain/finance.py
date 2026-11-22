"""Finance domain: classification of the public contour, formulas, balance, reconciliation.

Every number here is an integer number of kopecks. Unknown is ``None``; zero is a
confirmed zero. Nothing in this module derives a balance from a deficit, invents
fix-up transactions for historical errors, or counts a planned/out-of-scope
movement as public money.

See docs/DOMAIN.md for the contract used by the builder and the SSR layer.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .flows import EconomicClass, AccountScope, direction_for
from .money import apply_rate, share_percent, sum_kopecks
from .timeframes import Period, msk_day

CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

PAIR_KINDS = ("transfer_leg", "reimbursement")

PUBLIC_CATEGORY_KEY_PREFIX = "k"
UNCLASSIFIED_BUCKET = "unclassified"
UNCLASSIFIED_LABEL = "требуют классификации"
UNCLASSIFIED_KEY = "k_unclassified"

NON_OPERATING_VISIBLE = (
    EconomicClass.FINANCING_IN,
    EconomicClass.FINANCING_OUT,
    EconomicClass.BALANCE_ADJUSTMENT,
)


class MovementError(ValueError):
    """A normalized money movement that cannot be trusted."""


@dataclass(frozen=True)
class Movement:
    """A normalized money movement of the public accounting contour.

    ``source_account`` is where money leaves, ``target_account`` where it arrives;
    the two legs of one transfer are collapsed by :func:`build_ledger`.
    """

    id: str
    occurred_on: date
    amount_kopecks: int
    source_account: str | None = None
    target_account: str | None = None
    category_id: str | None = None
    currency: str = "RUB"
    pair_id: str | None = None
    pair_kind: str | None = None
    reimbursement_of: str | None = None
    is_planned: bool = False
    deleted: bool = False
    source_revision: int = 1
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise MovementError("movement id is required")
        if isinstance(self.amount_kopecks, bool) or not isinstance(self.amount_kopecks, int):
            raise MovementError("amount_kopecks must be an integer number of kopecks")
        if self.amount_kopecks <= 0:
            raise MovementError("amount_kopecks must be positive; direction comes from the accounts")
        if not isinstance(self.occurred_on, date) or isinstance(self.occurred_on, datetime):
            raise MovementError("occurred_on must be a fact date")
        if not CURRENCY_RE.match(self.currency):
            raise MovementError(f"unexpected currency {self.currency!r}")
        if self.source_account is None and self.target_account is None:
            raise MovementError("a movement needs at least one account")
        if self.pair_kind is not None and self.pair_kind not in PAIR_KINDS:
            raise MovementError(f"unknown pair_kind {self.pair_kind!r}")
        if self.pair_kind is not None and not self.pair_id:
            raise MovementError("paired movements need a pair_id")

    @property
    def direction(self) -> str:
        if self.source_account and self.target_account:
            return "internal"
        if self.source_account:
            return "out"
        return "in"


@dataclass(frozen=True)
class ScopePolicy:
    """Versioned public-contour mapping: accounts, categories, safe labels.

    Unknown accounts and unknown categories are never included automatically.
    """

    version: str = "policy"
    public_accounts: frozenset[str] = frozenset()
    private_accounts: frozenset[str] = frozenset()
    quarantined_accounts: frozenset[str] = frozenset()
    category_classes: Mapping[str, EconomicClass] = field(default_factory=dict)
    category_labels: Mapping[str, str] = field(default_factory=dict)
    quarantined_categories: frozenset[str] = frozenset()
    commission_categories: frozenset[str] = frozenset()
    subsidy_category_ids: frozenset[str] = frozenset()
    count_visibility_threshold_kopecks: int = 100_000
    sensitive_categories: frozenset[str] = frozenset()
    unclassified_label: str = UNCLASSIFIED_LABEL
    #: ``False`` when no confirmed map exists for the public contour at all (no
    #: recorded mapping, no private configuration).  The contour is then
    #: *unknown*: every indicator stays ``None`` — a missing map is not a zero
    #: and is not an invitation to count unmapped money as a donation.
    contour_confirmed: bool = True

    def account_scope(self, account_id: str) -> AccountScope:
        if account_id in self.public_accounts:
            return AccountScope.PUBLIC
        if account_id in self.private_accounts:
            return AccountScope.PRIVATE
        return AccountScope.UNKNOWN

    def class_for(self, category_id: str | None) -> EconomicClass | None:
        if category_id is None:
            return None
        if category_id in self.commission_categories:
            return EconomicClass.OPERATING_EXPENSE
        return self.category_classes.get(category_id)

    def label_for(self, category_id: str | None) -> str:
        if category_id is None:
            return self.unclassified_label
        return self.category_labels.get(category_id, self.unclassified_label)

    def reveals_operations_count(self, category_id: str | None, amount_kopecks: int) -> bool:
        if category_id is not None and category_id in self.sensitive_categories:
            return False
        return amount_kopecks >= self.count_visibility_threshold_kopecks


@dataclass(frozen=True)
class Classification:
    movement_id: str
    economic_class: EconomicClass
    scope: AccountScope
    publishable: bool
    reason: str
    amount_kopecks: int
    direction: str
    occurred_on: date
    category_id: str | None = None
    pair_id: str | None = None
    currency: str = "RUB"
    notes: tuple[str, ...] = ()


def _classification(
    movement: Movement,
    economic_class: EconomicClass,
    scope: AccountScope,
    publishable: bool,
    reason: str,
    notes: tuple[str, ...],
) -> Classification:
    return Classification(
        movement_id=movement.id,
        economic_class=economic_class,
        scope=scope,
        publishable=publishable,
        reason=reason,
        amount_kopecks=movement.amount_kopecks,
        direction=direction_for(economic_class, movement.direction),
        occurred_on=movement.occurred_on,
        category_id=movement.category_id,
        pair_id=movement.pair_id,
        currency=movement.currency,
        notes=notes,
    )


def classify_movement(movement: Movement, policy: ScopePolicy) -> Classification:
    accounts = [a for a in (movement.source_account, movement.target_account) if a]
    scopes = {policy.account_scope(a) for a in accounts}
    notes = tuple(dict.fromkeys(movement.notes))
    economic_class = policy.class_for(movement.category_id)

    if movement.reimbursement_of is not None and economic_class is EconomicClass.OPERATING_EXPENSE:
        # a commune member paying a bill directly is a funding source, not a second expense
        economic_class = EconomicClass.COMMUNE_SUBSIDY
        notes = tuple(dict.fromkeys(notes + ("reimbursement_of_expense",)))

    if AccountScope.PRIVATE in scopes:
        return _classification(
            movement,
            economic_class or EconomicClass.UNCLASSIFIED,
            AccountScope.PRIVATE,
            False,
            "account_outside_scope",
            notes,
        )

    if AccountScope.UNKNOWN in scopes:
        reason = "account_quarantined" if any(a in policy.quarantined_accounts for a in accounts) else "account_unknown"
        return _classification(
            movement,
            economic_class or EconomicClass.UNCLASSIFIED,
            AccountScope.UNKNOWN,
            False,
            reason,
            notes,
        )

    if movement.pair_kind == "transfer_leg":
        return _classification(
            movement, EconomicClass.INTERNAL_TRANSFER, AccountScope.PUBLIC, True, "internal_transfer", notes
        )

    if movement.category_id is not None and movement.category_id in policy.quarantined_categories:
        return _classification(
            movement, EconomicClass.UNCLASSIFIED, AccountScope.PUBLIC, False, "category_scope_unconfirmed", notes
        )

    if economic_class is None:
        return _classification(
            movement, EconomicClass.UNCLASSIFIED, AccountScope.PUBLIC, True, "new_category", notes
        )

    return _classification(movement, economic_class, AccountScope.PUBLIC, True, "category_mapping", notes)


@dataclass(frozen=True)
class ClassifiedLedger:
    period: Period
    policy: ScopePolicy
    data_present: bool
    contour_confirmed: bool
    classifications: tuple[Classification, ...]
    excluded: tuple[Classification, ...]
    quarantined: tuple[Classification, ...]
    planned_ids: tuple[str, ...]
    deleted_ids: tuple[str, ...]
    internal_transfer_pairs: tuple[str, ...]
    duplicated_legs: tuple[str, ...]
    transfer_notes: Mapping[str, tuple[str, ...]]
    orphan_transfer_legs: tuple[str, ...]
    reimbursements: tuple[str, ...]
    discovered_categories: tuple[str, ...]
    movement_count: int
    subsidy_evidence: bool

    @property
    def included_ids(self) -> tuple[str, ...]:
        return tuple(c.movement_id for c in self.classifications)

    @property
    def quarantined_ids(self) -> tuple[str, ...]:
        return tuple(c.movement_id for c in self.quarantined)

    @property
    def excluded_ids(self) -> tuple[str, ...]:
        return tuple(c.movement_id for c in self.excluded)

    @property
    def has_financing(self) -> bool:
        return any(c.economic_class in NON_OPERATING_VISIBLE for c in self.classifications)


def _collapse_transfer_legs(movements: Sequence[Movement]):
    groups: dict[str, list[Movement]] = defaultdict(list)
    for movement in movements:
        if movement.pair_kind == "transfer_leg" and movement.pair_id:
            groups[movement.pair_id].append(movement)

    synthetic: list[Movement] = []
    collapsed_ids: set[str] = set()
    duplicated_legs: list[str] = []
    transfer_notes: dict[str, tuple[str, ...]] = {}
    orphan: list[str] = []

    for pair_id, legs in groups.items():
        collapsed_ids.update(leg.id for leg in legs)
        if len(legs) < 2:
            orphan.append(pair_id)
            transfer_notes[pair_id] = ("single_sided_pair",)
            synthetic.append(replace(legs[0], pair_id=pair_id, notes=("single_sided_pair",)))
            continue
        out_legs = [leg for leg in legs if leg.source_account]
        in_legs = [leg for leg in legs if leg.target_account]
        base_out = out_legs[0] if out_legs else legs[0]
        base_in = in_legs[0] if in_legs else legs[-1]
        notes: list[str] = []
        if len({leg.currency for leg in legs}) > 1:
            notes.append("cross_currency")
        if any(leg.amount_kopecks != base_out.amount_kopecks for leg in legs):
            notes.append("pair_amount_mismatch")
        if not out_legs or not in_legs:
            notes.append("single_sided_pair")
        synthetic.append(
            Movement(
                id=f"transfer:{pair_id}",
                occurred_on=base_out.occurred_on,
                amount_kopecks=base_out.amount_kopecks,
                source_account=base_out.source_account,
                target_account=base_in.target_account,
                currency=base_out.currency,
                pair_id=pair_id,
                pair_kind="transfer_leg",
                notes=tuple(notes),
            )
        )
        duplicated_legs.append(pair_id)
        transfer_notes[pair_id] = tuple(notes)

    return synthetic, collapsed_ids, duplicated_legs, transfer_notes, orphan


def build_ledger(
    movements: Sequence[Movement],
    policy: ScopePolicy,
    period: Period,
    *,
    source_data_present: bool = True,
) -> ClassifiedLedger:
    """Classify the public contour for ``period``.

    ``source_data_present=False`` means the source was not read at all: every
    total stays unknown rather than collapsing to zero.
    """

    planned_ids: list[str] = []
    deleted_ids: list[str] = []
    live: list[Movement] = []
    for movement in movements:
        if movement.is_planned:
            planned_ids.append(movement.id)
            continue
        if movement.deleted:
            deleted_ids.append(movement.id)
            continue
        live.append(movement)

    in_period = [movement for movement in live if period.contains(movement.occurred_on)]
    synthetic, collapsed_ids, duplicated_legs, transfer_notes, orphan = _collapse_transfer_legs(in_period)
    others = [movement for movement in in_period if movement.id not in collapsed_ids]
    effective: list[Movement] = others + synthetic

    classifications: list[Classification] = []
    excluded: list[Classification] = []
    quarantined: list[Classification] = []
    for movement in effective:
        classified = classify_movement(movement, policy)
        if classified.scope is AccountScope.PRIVATE:
            excluded.append(classified)
        elif not classified.publishable:
            quarantined.append(classified)
        else:
            classifications.append(classified)

    discovered = sorted(
        {
            c.category_id
            for c in classifications
            if c.category_id is not None and policy.class_for(c.category_id) is None
        }
    )

    return ClassifiedLedger(
        period=period,
        policy=policy,
        data_present=source_data_present,
        contour_confirmed=policy.contour_confirmed,
        classifications=tuple(classifications),
        excluded=tuple(excluded),
        quarantined=tuple(quarantined),
        planned_ids=tuple(planned_ids),
        deleted_ids=tuple(deleted_ids),
        internal_transfer_pairs=tuple(
            sorted({c.pair_id for c in classifications if c.economic_class is EconomicClass.INTERNAL_TRANSFER and c.pair_id})
        ),
        duplicated_legs=tuple(duplicated_legs),
        transfer_notes=transfer_notes,
        orphan_transfer_legs=tuple(orphan),
        reimbursements=tuple(
            sorted({c.pair_id for c in classifications if c.pair_id and c.movement_id in _reimbursement_ids(effective)})
        ),
        discovered_categories=tuple(discovered),
        movement_count=len(movements),
        subsidy_evidence=bool(source_data_present and policy.subsidy_category_ids),
    )


def _reimbursement_ids(movements: Sequence[Movement]) -> frozenset[str]:
    return frozenset(m.id for m in movements if m.pair_kind == "reimbursement")


@dataclass(frozen=True)
class SubsidySignal:
    """Actual commune contribution: ``kopecks=None`` means unknown."""

    kopecks: int | None
    confirmed: bool
    source: str


def derive_subsidy(ledger: ClassifiedLedger) -> SubsidySignal:
    if not ledger.subsidy_evidence:
        return SubsidySignal(None, False, "not_tracked")
    total = sum_kopecks(
        c.amount_kopecks for c in ledger.classifications if c.economic_class is EconomicClass.COMMUNE_SUBSIDY
    )
    return SubsidySignal(total, True, "ledger")


@dataclass(frozen=True)
class CategoryTotal:
    """One public category row. ``category_id`` never leaves the domain layer."""

    category_id: str | None
    key: str
    label: str
    bucket: str
    direction: str
    amount_kopecks: int
    operations_count: int | None
    raw_operations_count: int
    share_percent: Decimal | None = None


def category_public_key(category_id: str | None) -> str:
    """Stable, non-revealing public key for a category (raw ids stay private)."""

    if category_id is None:
        return UNCLASSIFIED_KEY
    digest = hashlib.sha256(category_id.encode("utf-8")).hexdigest()
    return f"{PUBLIC_CATEGORY_KEY_PREFIX}{digest[:12]}"


@dataclass(frozen=True)
class FinanceTotals:
    period_label: str
    complete_period: bool
    categories: tuple[CategoryTotal, ...]
    expense_kopecks: int | None
    compensation_kopecks: int | None
    subsidy_kopecks: int | None
    uncovered_kopecks: int | None
    coverage_percent: Decimal | None
    coverage_is_final: bool
    shortfall_kopecks: int | None
    excess_kopecks: int | None
    result_before_subsidy_kopecks: int | None
    result_after_subsidy_kopecks: int | None
    gross_expense_kopecks: int | None
    expense_refund_kopecks: int | None
    external_compensation_kopecks: int | None
    compensation_refund_kopecks: int | None
    commune_subsidy_kopecks: int | None
    unclassified_in_kopecks: int | None
    unclassified_out_kopecks: int | None
    financing_in_kopecks: int | None
    financing_out_kopecks: int | None
    balance_adjustment_kopecks: int | None
    internal_transfer_kopecks: int | None
    category_sum_kopecks: int | None
    completeness: str
    labels: tuple[str, ...]


def _sum_class(classifications: Sequence[Classification], economic_class: EconomicClass) -> int:
    return sum_kopecks(c.amount_kopecks for c in classifications if c.economic_class is economic_class)


def _category_rows(classifications: Sequence[Classification], policy: ScopePolicy) -> tuple[CategoryTotal, ...]:
    # An unclassified category is never published one row per source category:
    # the owner has not approved those sums, so the per-category detail stays
    # private and what is published is the aggregate, split only by direction
    # (the same two numbers the unclassified totals already show).  A named
    # category, once the map confirms it, is published on its own row.
    grouped: dict[tuple[str | None, EconomicClass, str], list[Classification]] = defaultdict(list)
    for classification in classifications:
        if classification.economic_class is EconomicClass.INTERNAL_TRANSFER:
            continue  # transfers never change the money of the contour and are not categories
        if classification.economic_class is EconomicClass.UNCLASSIFIED:
            grouped[(None, classification.economic_class, classification.direction)].append(classification)
        else:
            grouped[(classification.category_id, classification.economic_class, "")].append(classification)

    bucket_totals: dict[str, int] = defaultdict(int)
    for (_category_id, economic_class, _direction), rows in grouped.items():
        bucket_totals[economic_class.value] += sum_kopecks(r.amount_kopecks for r in rows)

    totals: list[CategoryTotal] = []
    for (category_id, economic_class, _direction), rows in sorted(
        grouped.items(), key=lambda item: (item[0][1].value, item[0][0] or "", item[0][2])
    ):
        amount = sum_kopecks(r.amount_kopecks for r in rows)
        reveal = policy.reveals_operations_count(category_id, amount)
        totals.append(
            CategoryTotal(
                category_id=category_id,
                key=category_public_key(category_id),
                label=policy.label_for(category_id),
                bucket=economic_class.value,
                direction=direction_for(economic_class, rows[0].direction),
                amount_kopecks=amount,
                operations_count=len(rows) if reveal else None,
                raw_operations_count=len(rows),
                share_percent=share_percent(amount, bucket_totals[economic_class.value]),
            )
        )
    return tuple(totals)


def _unknown_totals(period_label: str, complete_period: bool, labels: tuple[str, ...], completeness: str) -> FinanceTotals:
    """Every indicator unknown: a missing source or a missing map is not a zero."""

    return FinanceTotals(
        period_label=period_label,
        complete_period=complete_period,
        categories=(),
        expense_kopecks=None,
        compensation_kopecks=None,
        subsidy_kopecks=None,
        uncovered_kopecks=None,
        coverage_percent=None,
        coverage_is_final=False,
        shortfall_kopecks=None,
        excess_kopecks=None,
        result_before_subsidy_kopecks=None,
        result_after_subsidy_kopecks=None,
        gross_expense_kopecks=None,
        expense_refund_kopecks=None,
        external_compensation_kopecks=None,
        compensation_refund_kopecks=None,
        commune_subsidy_kopecks=None,
        unclassified_in_kopecks=None,
        unclassified_out_kopecks=None,
        financing_in_kopecks=None,
        financing_out_kopecks=None,
        balance_adjustment_kopecks=None,
        internal_transfer_kopecks=None,
        category_sum_kopecks=None,
        completeness=completeness,
        labels=labels,
    )


def compute_totals(
    ledger: ClassifiedLedger,
    *,
    subsidy: SubsidySignal | None = None,
    as_of: date | None = None,
) -> FinanceTotals:
    """Apply the formulas of docs/REQUIREMENTS.md section 6.4."""

    period_label = ledger.period.label
    complete_period = ledger.period.is_complete(as_of) if as_of is not None else True

    if not ledger.data_present:
        return _unknown_totals(period_label, complete_period, ("no_data",), "unknown")

    if not ledger.contour_confirmed:
        # the public contour itself is unconfirmed (no recorded mapping, no
        # private configuration): no sum may be published as if it were the
        # contour's money — the indicators stay unknown with a visible warning
        labels = ["contour_unconfirmed"]
        if not complete_period:
            labels.append("period_incomplete")
        return _unknown_totals(period_label, complete_period, tuple(labels), "unknown")

    rows = _category_rows(ledger.classifications, ledger.policy)
    gross = _sum_class(ledger.classifications, EconomicClass.OPERATING_EXPENSE)
    expense_refunds = _sum_class(ledger.classifications, EconomicClass.EXPENSE_REFUND)
    external = _sum_class(ledger.classifications, EconomicClass.EXTERNAL_COMPENSATION)
    compensation_refunds = _sum_class(ledger.classifications, EconomicClass.COMPENSATION_REFUND)
    commune_subsidy = _sum_class(ledger.classifications, EconomicClass.COMMUNE_SUBSIDY)
    financing_in = _sum_class(ledger.classifications, EconomicClass.FINANCING_IN)
    financing_out = _sum_class(ledger.classifications, EconomicClass.FINANCING_OUT)
    adjustments = _sum_class(ledger.classifications, EconomicClass.BALANCE_ADJUSTMENT)
    internal = _sum_class(ledger.classifications, EconomicClass.INTERNAL_TRANSFER)

    unclassified_in = sum_kopecks(
        c.amount_kopecks
        for c in ledger.classifications
        if c.economic_class is EconomicClass.UNCLASSIFIED and c.direction == "in"
    )
    unclassified_out = sum_kopecks(
        c.amount_kopecks
        for c in ledger.classifications
        if c.economic_class is EconomicClass.UNCLASSIFIED and c.direction != "in"
    )

    expense = gross - expense_refunds
    compensation = external - compensation_refunds

    signal = subsidy if subsidy is not None else derive_subsidy(ledger)
    subsidy_kopecks = signal.kopecks if signal.confirmed else None

    uncovered = expense - compensation
    coverage = share_percent(compensation, expense) if expense > 0 else None

    labels: list[str] = []
    completeness = "complete"
    if ledger.quarantined:
        labels.append("quarantine_present")
        completeness = "partial"
    if unclassified_in or unclassified_out:
        labels.append("unclassified_present")
        completeness = "partial"
    if ledger.orphan_transfer_legs:
        labels.append("orphan_transfer_leg_present")
        completeness = "partial"
    if any("cross_currency" in c.notes for c in ledger.classifications):
        labels.append("cross_currency_transfer_present")
    if any("pair_amount_mismatch" in c.notes for c in ledger.classifications):
        labels.append("pair_amount_mismatch_present")
        completeness = "partial"
    if ledger.has_financing:
        labels.append("financing_present")
    if subsidy_kopecks is None:
        labels.append("subsidy_unknown")
        completeness = "partial"
    if expense == 0:
        labels.append("coverage_not_applicable")
    if expense < 0:
        labels.append("expense_refunds_exceed_expenses")
    if compensation < 0:
        labels.append("negative_net_compensation")
    if not complete_period:
        labels.append("period_incomplete")
        completeness = "partial"

    return FinanceTotals(
        period_label=period_label,
        complete_period=complete_period,
        categories=rows,
        expense_kopecks=expense,
        compensation_kopecks=compensation,
        subsidy_kopecks=subsidy_kopecks,
        uncovered_kopecks=uncovered,
        coverage_percent=coverage,
        coverage_is_final=completeness == "complete" and coverage is not None,
        shortfall_kopecks=max(uncovered, 0),
        excess_kopecks=max(-uncovered, 0),
        result_before_subsidy_kopecks=compensation - expense,
        result_after_subsidy_kopecks=(compensation + subsidy_kopecks - expense) if subsidy_kopecks is not None else None,
        gross_expense_kopecks=gross,
        expense_refund_kopecks=expense_refunds,
        external_compensation_kopecks=external,
        compensation_refund_kopecks=compensation_refunds,
        commune_subsidy_kopecks=commune_subsidy,
        unclassified_in_kopecks=unclassified_in,
        unclassified_out_kopecks=unclassified_out,
        financing_in_kopecks=financing_in,
        financing_out_kopecks=financing_out,
        balance_adjustment_kopecks=adjustments,
        internal_transfer_kopecks=internal,
        category_sum_kopecks=sum_kopecks(row.amount_kopecks for row in rows),
        completeness=completeness,
        labels=tuple(labels),
    )


@dataclass(frozen=True)
class CheckResult:
    code: str
    severity: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


def reconcile_totals(ledger: ClassifiedLedger, totals: FinanceTotals) -> tuple[CheckResult, ...]:
    """Reconcile category rows with the aggregate fields of the same query."""

    if not ledger.data_present or not ledger.contour_confirmed:
        return ()

    expected = {
        EconomicClass.OPERATING_EXPENSE.value: totals.gross_expense_kopecks,
        EconomicClass.EXPENSE_REFUND.value: totals.expense_refund_kopecks,
        EconomicClass.EXTERNAL_COMPENSATION.value: totals.external_compensation_kopecks,
        EconomicClass.COMPENSATION_REFUND.value: totals.compensation_refund_kopecks,
        EconomicClass.COMMUNE_SUBSIDY.value: totals.commune_subsidy_kopecks,
        EconomicClass.FINANCING_IN.value: totals.financing_in_kopecks,
        EconomicClass.FINANCING_OUT.value: totals.financing_out_kopecks,
        EconomicClass.BALANCE_ADJUSTMENT.value: totals.balance_adjustment_kopecks,
    }
    actual: dict[str, int] = defaultdict(int)
    for row in totals.categories:
        actual[row.bucket] += row.amount_kopecks
    actual[UNCLASSIFIED_BUCKET] = (totals.unclassified_in_kopecks or 0) + (totals.unclassified_out_kopecks or 0)

    checks: list[CheckResult] = []
    for bucket, value in expected.items():
        if value is None:
            continue
        if actual.get(bucket, 0) != value:
            checks.append(
                CheckResult(
                    code="category_sum_mismatch",
                    severity="error",
                    message=f"category rows for {bucket} do not match the {bucket} aggregate",
                    details={"bucket": bucket, "rows_kopecks": actual.get(bucket, 0), "aggregate_kopecks": value},
                )
            )
    rows_total = sum_kopecks(row.amount_kopecks for row in totals.categories)
    if rows_total != totals.category_sum_kopecks:
        checks.append(
            CheckResult(
                code="category_sum_mismatch",
                severity="error",
                message="category rows do not sum to the reported category sum",
                details={"rows_kopecks": rows_total, "reported_kopecks": totals.category_sum_kopecks},
            )
        )
    return tuple(checks)


def reconcile_year_vs_months(
    months: Mapping[str, FinanceTotals],
    year: FinanceTotals,
) -> tuple[CheckResult, ...]:
    """Compare a year aggregate with the sum of its months at equal coverage."""

    checks: list[CheckResult] = []
    available = [t for t in months.values() if t.expense_kopecks is not None]
    incomplete = [t for t in available if t.completeness != "complete"]

    fields = (
        "expense_kopecks",
        "compensation_kopecks",
        "gross_expense_kopecks",
        "expense_refund_kopecks",
        "external_compensation_kopecks",
        "compensation_refund_kopecks",
    )
    for name in fields:
        year_value = getattr(year, name)
        if year_value is None:
            continue
        month_values = [getattr(month, name) for month in available]
        if any(value is None for value in month_values):
            continue
        month_sum = sum_kopecks(month_values)
        if month_sum != year_value:
            checks.append(
                CheckResult(
                    code="month_year_mismatch",
                    severity="error",
                    message=f"months do not add up to the year for {name}",
                    details={"field": name, "months_kopecks": month_sum, "year_kopecks": year_value},
                )
            )
    if incomplete:
        checks.append(
            CheckResult(
                code="month_year_incomplete",
                severity="warning",
                message="some months are incomplete; the comparison is provisional",
                details={"incomplete_months": len(incomplete)},
            )
        )
    if not months:
        checks.append(
            CheckResult(
                code="month_year_missing",
                severity="warning",
                message="no monthly aggregates available for the year",
                details={"months": 0},
            )
        )
    return tuple(checks)


def compare_historical(
    published_kopecks: int | None,
    computed_kopecks: int | None,
    *,
    label: str,
    period_label: str | None = None,
) -> CheckResult:
    """Compare an original published value with the recomputed one.

    The original publication stays fixed; a difference is surfaced as a visible
    conflict and never "fixed" by inventing transactions.
    """

    details: dict[str, Any] = {"label": label, "period": period_label}
    if published_kopecks is None and computed_kopecks is None:
        return CheckResult("historical_missing", "warning", f"no published and no computed value for {label}", details)
    if published_kopecks is None:
        details["computed_kopecks"] = computed_kopecks
        return CheckResult(
            "historical_no_published", "info", f"no original publication found for {label}", details
        )
    if computed_kopecks is None:
        details["published_kopecks"] = published_kopecks
        return CheckResult(
            "historical_computed_missing", "warning", f"recomputed value is unknown for {label}", details
        )
    if published_kopecks == computed_kopecks:
        details["value_kopecks"] = published_kopecks
        return CheckResult("historical_match", "info", f"{label} matches the original publication", details)
    details.update(
        published_kopecks=published_kopecks,
        computed_kopecks=computed_kopecks,
        difference_kopecks=published_kopecks - computed_kopecks,
    )
    return CheckResult(
        "historical_conflict",
        "warning",
        f"{label}: the original publication stays fixed, the recomputed value differs and is shown as a conflict",
        details,
    )


def weighted_annual_coverage(totals: Sequence[FinanceTotals]) -> Decimal | None:
    """Annual coverage is the ratio of annual sums, never the mean of monthly percentages."""

    expenses = [t.expense_kopecks for t in totals if t.expense_kopecks is not None]
    compensations = [t.compensation_kopecks for t in totals if t.compensation_kopecks is not None]
    if not expenses or not compensations:
        return None
    return share_percent(sum_kopecks(compensations), sum_kopecks(expenses))


@dataclass(frozen=True)
class AccountBalance:
    account_id: str
    currency: str = "RUB"
    balance_kopecks: int = 0
    as_of: date | None = None
    is_liquid: bool = True
    confirmed: bool = True
    scope: AccountScope = AccountScope.PUBLIC


@dataclass(frozen=True)
class Liability:
    id: str
    amount_kopecks: int | None
    due_on: date | None = None
    label: str = ""


@dataclass(frozen=True)
class BalanceSummary:
    kopecks: int | None
    as_of: date | None
    completeness: str
    labels: tuple[str, ...]
    included_accounts: tuple[str, ...]
    excluded_accounts: tuple[str, ...]
    obligations_kopecks: int | None
    converted_accounts: tuple[str, ...] = ()


def consolidated_balance(
    balances: Iterable[AccountBalance],
    *,
    as_of: date | None = None,
    rates: Mapping[str, Decimal] | None = None,
    liabilities: Iterable[Liability] = (),
    scope_policy: ScopePolicy | None = None,
) -> BalanceSummary:
    """Sum confirmed liquid public balances as of one date.

    Never derived from a deficit, from obligations or from the current balance of
    another date. An unknown rate or an unknown account makes the sum unknown.
    """

    candidates = [b for b in balances if as_of is None or b.as_of == as_of]
    selected: dict[str, AccountBalance] = {}
    for balance in candidates:
        current = selected.get(balance.account_id)
        if current is None:
            selected[balance.account_id] = balance
            continue
        if (balance.as_of or date.min) > (current.as_of or date.min):
            selected[balance.account_id] = balance
        elif balance.as_of == current.as_of and balance.confirmed and not current.confirmed:
            selected[balance.account_id] = balance

    liabilities = tuple(liabilities)
    labels: list[str] = []
    included: list[str] = []
    excluded: list[str] = []
    converted: list[str] = []

    if liabilities:
        labels.append("liabilities_separate")
        amounts = [l.amount_kopecks for l in liabilities]
        obligations: int | None = None if any(a is None for a in amounts) else sum_kopecks(amounts)
        if obligations is None:
            labels.append("obligations_unknown")
    else:
        obligations = None

    total = 0
    unknown_conversion = False
    for account_id, balance in sorted(selected.items()):
        scope = balance.scope
        if scope_policy is not None:
            scope = scope_policy.account_scope(account_id)
        if scope is not AccountScope.PUBLIC:
            excluded.append(account_id)
            labels.append("account_outside_scope")
            continue
        if not balance.is_liquid:
            excluded.append(account_id)
            labels.append("not_liquid_excluded")
            continue
        if not balance.confirmed:
            excluded.append(account_id)
            labels.append("unconfirmed_snapshot_excluded")
            continue
        if balance.currency == "RUB":
            total += balance.balance_kopecks
        else:
            rate = (rates or {}).get(balance.currency)
            if rate is None:
                unknown_conversion = True
                excluded.append(account_id)
                labels.append("no_rate")
                continue
            total += apply_rate(balance.balance_kopecks, rate)
            converted.append(account_id)
            labels.append("converted")
        included.append(account_id)

    summary_as_of = as_of
    if summary_as_of is None:
        dates = [b.as_of for b in selected.values() if b.as_of is not None]
        summary_as_of = max(dates) if dates else None
    ordered_labels = tuple(dict.fromkeys(labels))

    if unknown_conversion:
        return BalanceSummary(
            None,
            summary_as_of,
            "partial",
            tuple(dict.fromkeys(ordered_labels + ("balance_incomplete",))),
            tuple(included),
            tuple(excluded),
            obligations,
            tuple(converted),
        )
    if not included:
        return BalanceSummary(
            None,
            summary_as_of,
            "unknown",
            tuple(dict.fromkeys(ordered_labels + ("unknown_historical_balance",))),
            (),
            tuple(excluded),
            obligations,
            (),
        )
    return BalanceSummary(
        total,
        summary_as_of,
        "complete",
        ordered_labels,
        tuple(included),
        tuple(excluded),
        obligations,
        tuple(converted),
    )
