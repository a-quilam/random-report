"""Versioned owner allocation overrides (the basement's confirmed share).

The owner confirms, once, how the basement's own money is computed, and that
decision is a *versioned* document, not a per-report edit:

* **excluded categories** — a category the owner keeps out of the report: it
  leaves no trace in any total (not even the aggregate fields);
* **shared expenses** — for common costs and utilities the basement bears a
  confirmed fraction (the owner confirmed one third).  The fraction is applied
  to the *monthly* sum of whole kopecks with an explicit rounding rule, so a
  year is the sum of its months and never a fraction of the yearly total;
* **rent** — the report carries the agreed 90 000 ₽/month instead of the actual
  bank payment, and the two are shown *side by side*, never added together.  The
  rule is never extrapolated into a month the source has no rent for, and a
  month before the rule's ``from_month`` keeps the real payment (a closed report
  is frozen and is never rewritten backwards);
* **corrections** — the owner's manual adjustments of *both* signs, kept
  separately; a correction whose article has no confirmed class is published as
  unclassified, never with an invented class;
* **supercategories** — a parent category that rolls up its children must not
  double them: the parent contributes only the part its children do not cover.

The policy is stored inside the confirmed public-contour mapping payload under
``allocation`` (so a desk edit is a new version of one atomic document and the
same rollback machinery applies).  The reversible per-operation exclusion is a
desk decision kept in its own table; the loader folds the active rows into a
fresh :class:`AllocationPolicy` — the source transactions are never touched.

The public project never sees the raw category ids: an adjustment names its
category only by its non-revealing public key.  See docs/ALLOCATION.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

ALLOCATION_VERSION = "allocation"

#: The explicit rounding rules a share may use.  ``half_up`` is the default the
#: owner's rules use; the others exist so a rule can be stated unambiguously.
ROUNDING_MODES: Mapping[str, str] = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
    "down": ROUND_DOWN,
}

#: Adjustment kinds, in the order they are applied to a ledger.
ADJUSTMENT_KINDS = (
    "exclude_operation",
    "exclude_category",
    "share",
    "rent",
    "supercategory",
    "correction",
)

STATUS_LABELS = {
    "applied": "allocation_applied",
    "excluded_category": "excluded_category_present",
    "excluded_operation": "excluded_operation_present",
    "share": "shared_expense_present",
    "rent": "rent_replaced_by_agreed_amount",
    "rent_differs": "rent_bank_payment_differs",
    "corrections": "owner_corrections_present",
    "correction_unclassified": "correction_unclassified_article",
    "supercategory": "supercategory_rollup_adjusted",
}


class AllocationError(ValueError):
    """An allocation rule that cannot be trusted."""


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def last_day(month: str) -> date:
    match = MONTH_RE.match(month)
    if not match:
        raise AllocationError(f"не месяц: {month!r}")
    year, month_number = int(match.group(1)), int(match.group(2))
    if month_number == 12:
        return date(year, 12, 31)
    return date(year, month_number + 1, 1) - timedelta(days=1)


def _require_month(value: str) -> str:
    text = str(value or "").strip()
    if not MONTH_RE.match(text):
        raise AllocationError(f"период правила должен быть целым месяцем: {value!r}")
    return text


def _require_int(value: Any, *, what: str, positive: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AllocationError(f"{what}: ожидается целое число копеек")
    if positive and value <= 0:
        raise AllocationError(f"{what}: ожидается положительное число копеек")
    return value


@dataclass(frozen=True)
class ShareRule:
    """A confirmed fraction of a shared expense, applied monthly."""

    numerator: int
    denominator: int
    rounding: str = "half_up"

    def __post_init__(self) -> None:
        _require_int(self.numerator, what="числитель доли")
        _require_int(self.denominator, what="знаменатель доли")
        if self.rounding not in ROUNDING_MODES:
            raise AllocationError(f"неизвестное правило округления {self.rounding!r}")

    @property
    def label(self) -> str:
        return f"{self.numerator}/{self.denominator}"

    def apply(self, amount_kopecks: int) -> int:
        """Apply the fraction to a monthly sum of whole kopecks.

        The arithmetic is decimal and the result is quantized to whole kopecks
        with the rule's rounding, so the same input always yields the same
        output (never a float, never a different tie-break).
        """

        if amount_kopecks < 0:
            raise AllocationError("доля применяется к неотрицательной месячной сумме")
        mode = ROUNDING_MODES[self.rounding]
        value = (Decimal(amount_kopecks) * Decimal(self.numerator) / Decimal(self.denominator)).quantize(
            Decimal(1), rounding=mode
        )
        return int(value)


@dataclass(frozen=True)
class RentRule:
    """The agreed monthly rent, replacing the actual payment in the report."""

    monthly_kopecks: int
    #: ``None`` means \"every month that has a real rent movement\".  A month
    #: before this boundary keeps the actual bank payment, so an old period is
    #: never rewritten by a rule that did not exist then.
    from_month: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.monthly_kopecks, what="расчётная аренда")
        if self.from_month is not None:
            object.__setattr__(self, "from_month", _require_month(self.from_month))

    def applies_to(self, month: str) -> bool:
        if self.from_month is None:
            return True
        return month >= self.from_month


@dataclass(frozen=True)
class Correction:
    """One signed owner correction for a whole month."""

    period: str
    amount_kopecks: int
    category_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "period", _require_month(self.period))
        _require_int(self.amount_kopecks, what="корректировка", positive=False)
        if self.amount_kopecks == 0:
            raise AllocationError("пустая корректировка: сумма должна отличаться от нуля")


def _parse_share(value: Any, *, owner: str) -> ShareRule:
    if not isinstance(value, Mapping):
        raise AllocationError(f"{owner}: доля должна быть объектом")
    numerator = value.get("numerator", value.get("share"))
    denominator = value.get("denominator")
    if denominator is None:
        # a bare "share" is read as the numerator over 100 only when it is a
        # fraction such as 1/3 spelled out with an explicit denominator
        raise AllocationError(f"{owner}: у доли нужен знаменатель")
    return ShareRule(
        numerator=_require_int(numerator, what=f"{owner}: числитель"),
        denominator=_require_int(denominator, what=f"{owner}: знаменатель"),
        rounding=str(value.get("rounding") or "half_up"),
    )


@dataclass(frozen=True)
class AllocationPolicy:
    """The owner's confirmed allocation overrides, or an inert empty policy."""

    version: str = ALLOCATION_VERSION
    confirmed: bool = True
    excluded_category_ids: frozenset[str] = frozenset()
    excluded_transaction_ids: frozenset[str] = frozenset()
    share_rules: Mapping[str, ShareRule] = field(default_factory=dict)
    rent_category_ids: frozenset[str] = frozenset()
    rent_rule: RentRule | None = None
    supercategories: Mapping[str, frozenset[str]] = field(default_factory=dict)
    corrections: tuple[Correction, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.excluded_category_ids
            or self.excluded_transaction_ids
            or self.share_rules
            or self.rent_rule
            or self.supercategories
            or self.corrections
        )

    def with_exclusions(self, transaction_ids: Sequence[str]) -> "AllocationPolicy":
        """A copy carrying the active reversible per-operation exclusions."""

        return replace(self, excluded_transaction_ids=frozenset(str(item) for item in transaction_ids if str(item)))

    def correction_totals(self) -> tuple[int, int]:
        """Positive and negative corrections, kept separately (never netted)."""

        positives = sum(c.amount_kopecks for c in self.corrections if c.amount_kopecks > 0)
        negatives = sum(c.amount_kopecks for c in self.corrections if c.amount_kopecks < 0)
        return positives, negatives


def allocation_policy_from_payload(version: str, payload: Mapping[str, Any]) -> AllocationPolicy:
    """Rebuild an :class:`AllocationPolicy` from a stored mapping payload.

    An unknown rounding, a float or a malformed month is refused outright: a
    typo must never silently widen what the report shows.
    """

    if payload in (None, {}):
        return AllocationPolicy(version=version or ALLOCATION_VERSION)

    excluded_categories = frozenset(str(item) for item in (payload.get("excluded_category_ids") or ()) if str(item))
    excluded_transactions = frozenset(
        str(item) for item in (payload.get("excluded_transaction_ids") or ()) if str(item)
    )

    share_rules: dict[str, ShareRule] = {}
    for category_id, raw in (payload.get("share_rules") or {}).items():
        if not str(category_id):
            raise AllocationError("share_rules: ключ категории не может быть пустым")
        share_rules[str(category_id)] = _parse_share(raw, owner=f"share_rules[{category_id}]")

    rent_raw = payload.get("rent")
    rent_rule = None
    if rent_raw:
        if not isinstance(rent_raw, Mapping):
            raise AllocationError("rent: правило аренды должно быть объектом")
        rent_rule = RentRule(
            monthly_kopecks=_require_int(rent_raw.get("monthly_kopecks"), what="rent.monthly_kopecks"),
            from_month=(str(rent_raw["from_month"]) if rent_raw.get("from_month") else None),
        )
    rent_categories = frozenset(str(item) for item in (payload.get("rent_category_ids") or ()) if str(item))

    supercategories: dict[str, frozenset[str]] = {}
    for parent, children in (payload.get("supercategories") or {}).items():
        children_ids = frozenset(str(item) for item in (children or ()) if str(item))
        if not str(parent) or not children_ids:
            raise AllocationError("supercategories: нужен родитель и хотя бы один дочерний элемент")
        supercategories[str(parent)] = children_ids

    corrections: list[Correction] = []
    for raw in payload.get("corrections") or ():
        if not isinstance(raw, Mapping):
            raise AllocationError("corrections: запись должна быть объектом")
        reason = str(raw.get("reason") or "")
        if len(reason) > 200:
            raise AllocationError("corrections: причина длиннее 200 символов")
        corrections.append(
            Correction(
                period=str(raw.get("period") or ""),
                amount_kopecks=_require_int(raw.get("amount_kopecks"), what="corrections.amount_kopecks", positive=False),
                category_id=(str(raw["category_id"]) if raw.get("category_id") else None),
                reason=reason,
            )
        )

    return AllocationPolicy(
        version=str(version or ALLOCATION_VERSION),
        confirmed=bool(payload.get("confirmed", True)),
        excluded_category_ids=excluded_categories,
        excluded_transaction_ids=excluded_transactions,
        share_rules=share_rules,
        rent_category_ids=rent_categories,
        rent_rule=rent_rule,
        supercategories=supercategories,
        corrections=tuple(corrections),
    )


@dataclass(frozen=True)
class AllocationAdjustment:
    """One applied override, described for the public explanation."""

    kind: str
    category_key: str
    period: str | None
    before_kopecks: int
    after_kopecks: int
    note: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "category_key": self.category_key,
            "period": self.period,
            "before_kopecks": self.before_kopecks,
            "after_kopecks": self.after_kopecks,
            "note": self.note,
        }


@dataclass(frozen=True)
class RentMonth:
    month: str
    computed_kopecks: int
    bank_kopecks: int
    applied: bool
    note: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "computed_kopecks": self.computed_kopecks,
            "bank_kopecks": self.bank_kopecks,
            "applied": self.applied,
            "note": self.note,
        }


@dataclass(frozen=True)
class AllocationReport:
    """What the overrides did, in public-safe terms (public keys and counts)."""

    version: str
    confirmed: bool
    applied: bool
    excluded_categories: tuple[str, ...] = ()
    excluded_operations: int = 0
    shared_expenses_before_kopecks: int = 0
    shared_expenses_after_kopecks: int = 0
    rent_months: tuple[RentMonth, ...] = ()
    computed_rent_kopecks: int = 0
    bank_rent_kopecks: int = 0
    corrections_positive_kopecks: int = 0
    corrections_negative_kopecks: int = 0
    corrections_unclassified: bool = False
    adjustments: tuple[AllocationAdjustment, ...] = ()
    labels: tuple[str, ...] = ()

    @property
    def rent_applied(self) -> bool:
        return any(month.applied for month in self.rent_months)

    @property
    def rent_differs(self) -> bool:
        return self.rent_applied and self.computed_rent_kopecks != self.bank_rent_kopecks

    def to_public(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "confirmed": self.confirmed,
            "applied": self.applied,
            "excluded_categories": len(self.excluded_categories),
            "excluded_operations": self.excluded_operations,
            "shared_expenses_before_kopecks": self.shared_expenses_before_kopecks,
            "shared_expenses_after_kopecks": self.shared_expenses_after_kopecks,
            "computed_rent_kopecks": self.computed_rent_kopecks,
            "bank_rent_kopecks": self.bank_rent_kopecks,
            "rent_applied": self.rent_applied,
            "rent_differs": self.rent_differs,
            "corrections_positive_kopecks": self.corrections_positive_kopecks,
            "corrections_negative_kopecks": self.corrections_negative_kopecks,
            "corrections_unclassified": self.corrections_unclassified,
            "adjustments": [item.to_public() for item in self.adjustments],
            "rent_months": [item.to_public() for item in self.rent_months],
            "labels": list(self.labels),
        }

    @classmethod
    def inert(cls, version: str = ALLOCATION_VERSION, *, confirmed: bool = True) -> "AllocationReport":
        return cls(version=version, confirmed=confirmed, applied=False)


def _groups(classifications: Sequence[Any], key) -> dict[Any, list[Any]]:
    grouped: dict[Any, list[Any]] = {}
    for classification in classifications:
        grouped.setdefault(key(classification), []).append(classification)
    return grouped


def _reclassify(
    classification: Any,
    *,
    amount_kopecks: int,
    movement_id: str,
    reason: str,
    notes: tuple[str, ...],
) -> Any:
    return replace(
        classification,
        movement_id=movement_id,
        amount_kopecks=amount_kopecks,
        reason=reason,
        notes=tuple(dict.fromkeys(tuple(classification.notes) + notes)),
    )


def apply_allocation(ledger: Any, allocation: AllocationPolicy, *, policy: Any = None) -> tuple[Any, AllocationReport]:
    """Fold the owner overrides into a classified ledger.

    Returns ``(allocated_ledger, report)``.  The allocated ledger's
    ``classifications`` are what the totals are computed from, so every
    aggregate, CSV row and HTML row stays consistent by construction; the report
    carries the explanation (which category was excluded, how a share was
    rounded, how the agreed rent compares with the bank payment).
    """

    from .finance import EconomicClass, category_public_key
    from .flows import direction_for
    from .timeframes import ALL_TIME

    period = ledger.period
    months = period.months()
    all_time = period.kind == ALL_TIME

    def month_inside(month: str) -> bool:
        return all_time or month in months

    base = list(ledger.classifications)
    adjustments: list[AllocationAdjustment] = []
    labels: list[str] = []

    # 1. reversible per-operation exclusion ---------------------------------- #
    if allocation.excluded_transaction_ids:
        removed = [c for c in base if c.movement_id in allocation.excluded_transaction_ids]
        base = [c for c in base if c.movement_id not in allocation.excluded_transaction_ids]
        if removed:
            labels.append(STATUS_LABELS["excluded_operation"])
    else:
        removed = []
    excluded_operations = len(removed)

    # 2. excluded categories: they leave no trace in any total -------------- #
    excluded_category_keys: list[str] = []
    if allocation.excluded_category_ids:
        kept: list[Any] = []
        for classification in base:
            if classification.category_id is not None and classification.category_id in allocation.excluded_category_ids:
                excluded_category_keys.append(category_public_key(classification.category_id))
            else:
                kept.append(classification)
        base = kept
        excluded_category_keys = sorted(set(excluded_category_keys))
        if excluded_category_keys:
            labels.append(STATUS_LABELS["excluded_category"])

    # 3. shared expenses: monthly aggregate, explicit rounding -------------- #
    share_before = share_after = 0
    if allocation.share_rules:
        touched = False
        for category_id, rule in sorted(allocation.share_rules.items()):
            grouped = _groups(
                [c for c in base if c.category_id == category_id],
                key=lambda c: month_of(c.occurred_on),
            )
            for month, rows in sorted(grouped.items()):
                total = sum(row.amount_kopecks for row in rows)
                allocated = rule.apply(total)
                share_before += total
                share_after += allocated
                adjustments.append(
                    AllocationAdjustment(
                        kind="share",
                        category_key=category_public_key(category_id),
                        period=month,
                        before_kopecks=total,
                        after_kopecks=allocated,
                        note=f"доля {rule.label}, округление {rule.rounding}",
                    )
                )
                base = [c for c in base if c.movement_id not in {row.movement_id for row in rows}]
                if allocated > 0:
                    base.append(
                        _reclassify(
                            rows[0],
                            amount_kopecks=allocated,
                            movement_id=f"alloc:{category_id}:{month}",
                            reason="allocation_share",
                            notes=("shared_expense_share", f"share_{rule.label}"),
                        )
                    )
                touched = True
        if touched:
            labels.append(STATUS_LABELS["share"])

    # 4. rent: the agreed amount replaces the bank payment ------------------ #
    rent_months: list[RentMonth] = []
    computed_rent = bank_rent = 0
    if allocation.rent_rule is not None and allocation.rent_category_ids:
        rows = [c for c in base if c.category_id in allocation.rent_category_ids]
        grouped = _groups(rows, key=lambda c: month_of(c.occurred_on))
        for month, group in sorted(grouped.items()):
            bank = sum(row.amount_kopecks for row in group)
            bank_rent += bank
            if not allocation.rent_rule.applies_to(month):
                rent_months.append(
                    RentMonth(month=month, computed_kopecks=0, bank_kopecks=bank, applied=False, note="before_rule")
                )
                continue
            computed = allocation.rent_rule.monthly_kopecks
            computed_rent += computed
            rent_months.append(
                RentMonth(month=month, computed_kopecks=computed, bank_kopecks=bank, applied=True)
            )
            base = [c for c in base if c.movement_id not in {row.movement_id for row in group}]
            base.append(
                _reclassify(
                    group[0],
                    amount_kopecks=computed,
                    movement_id=f"alloc:rent:{month}",
                    reason="allocation_rent",
                    notes=("rent_fixed_amount", "bank_payment_shown_separately"),
                )
            )
            adjustments.append(
                AllocationAdjustment(
                    kind="rent",
                    category_key=category_public_key(group[0].category_id),
                    period=month,
                    before_kopecks=bank,
                    after_kopecks=computed,
                    note="банковский платёж показан отдельно",
                )
            )
        if computed_rent:
            labels.append(STATUS_LABELS["rent"])
        if computed_rent and computed_rent != bank_rent:
            labels.append(STATUS_LABELS["rent_differs"])

    # 5. supercategories must not double their children --------------------- #
    if allocation.supercategories:
        touched = False
        for parent, children in sorted(allocation.supercategories.items()):
            parent_rows = [c for c in base if c.category_id == parent]
            if not parent_rows:
                continue
            grouped = _groups(
                parent_rows,
                key=lambda c: (month_of(c.occurred_on), c.economic_class, c.direction),
            )
            for (month, economic_class, direction), group in sorted(grouped.items(), key=lambda item: str(item[0])):
                children_total = sum(
                    c.amount_kopecks
                    for c in base
                    if c.category_id in children
                    and c.economic_class == economic_class
                    and c.direction == direction
                    and month_of(c.occurred_on) == month
                )
                parent_total = sum(c.amount_kopecks for c in group)
                extra = parent_total - children_total
                base = [c for c in base if c.movement_id not in {row.movement_id for row in group}]
                if extra > 0:
                    base.append(
                        _reclassify(
                            group[0],
                            amount_kopecks=extra,
                            movement_id=f"alloc:super:{parent}:{month}:{economic_class.value}",
                            reason="allocation_supercategory",
                            notes=("supercategory_own_part",),
                        )
                    )
                adjustments.append(
                    AllocationAdjustment(
                        kind="supercategory",
                        category_key=category_public_key(parent),
                        period=month,
                        before_kopecks=parent_total,
                        after_kopecks=max(extra, 0),
                        note=("надкатегория не задваивает дочерние статьи"),
                    )
                )
                touched = True
        if touched:
            labels.append(STATUS_LABELS["supercategory"])

    # 6. corrections of both signs, kept separately ------------------------- #
    positives = negatives = 0
    correction_unclassified = False
    for correction in allocation.corrections:
        if not month_inside(correction.period):
            continue
        if correction.amount_kopecks > 0:
            positives += correction.amount_kopecks
        else:
            negatives += correction.amount_kopecks
        economic_class = None
        if correction.category_id is not None and policy is not None:
            economic_class = policy.class_for(correction.category_id)
        notes: list[str] = ["owner_correction"]
        if economic_class is None:
            # an article with no confirmed class keeps its own sign; no class is
            # ever invented for it
            economic_class = EconomicClass.UNCLASSIFIED
            correction_unclassified = True
            notes.append("correction_unclassified")
            direction = "in" if correction.amount_kopecks > 0 else "out"
        else:
            direction = direction_for(economic_class)
        base.append(
            _make_classification(
                movement_id=f"alloc:corr:{correction.period}:{correction.category_id or 'unclassified'}:{correction.amount_kopecks}",
                occurred_on=last_day(correction.period),
                amount_kopecks=correction.amount_kopecks,
                economic_class=economic_class,
                direction=direction,
                category_id=correction.category_id,
                reason="allocation_correction",
                notes=tuple(notes),
            )
        )
        adjustments.append(
            AllocationAdjustment(
                kind="correction",
                category_key=category_public_key(correction.category_id),
                period=correction.period,
                before_kopecks=0,
                after_kopecks=correction.amount_kopecks,
                # the operator's free-text reason stays private: only the fact,
                # the month, the category key and the amount are published
                note="статья без подтверждённого класса" if correction_unclassified else "корректировка подвала",
            )
        )
    if positives or negatives:
        labels.append(STATUS_LABELS["corrections"])
    if correction_unclassified:
        labels.append(STATUS_LABELS["correction_unclassified"])

    applied = bool(
        excluded_operations
        or excluded_category_keys
        or share_before
        or computed_rent
        or positives
        or negatives
        or adjustments
    )
    if applied:
        labels.append(STATUS_LABELS["applied"])

    allocated = replace(ledger, classifications=tuple(base))
    report = AllocationReport(
        version=allocation.version,
        confirmed=allocation.confirmed and ledger.contour_confirmed,
        applied=applied,
        excluded_categories=tuple(excluded_category_keys),
        excluded_operations=excluded_operations,
        shared_expenses_before_kopecks=share_before,
        shared_expenses_after_kopecks=share_after,
        rent_months=tuple(rent_months),
        computed_rent_kopecks=computed_rent,
        bank_rent_kopecks=bank_rent,
        corrections_positive_kopecks=positives,
        corrections_negative_kopecks=negatives,
        corrections_unclassified=correction_unclassified,
        adjustments=tuple(adjustments),
        labels=tuple(dict.fromkeys(labels)),
    )
    return allocated, report


def _make_classification(**kwargs: Any) -> Any:
    """Build a :class:`randreport.domain.finance.Classification` without guessing."""

    from .finance import AccountScope, Classification

    notes = kwargs.pop("notes", ())
    return Classification(
        scope=AccountScope.PUBLIC,
        publishable=True,
        notes=tuple(notes),
        **kwargs,
    )


__all__ = [
    "ADJUSTMENT_KINDS",
    "ALLOCATION_VERSION",
    "ROUNDING_MODES",
    "STATUS_LABELS",
    "AllocationAdjustment",
    "AllocationError",
    "AllocationPolicy",
    "AllocationReport",
    "Correction",
    "RentMonth",
    "RentRule",
    "ShareRule",
    "allocation_policy_from_payload",
    "apply_allocation",
    "last_day",
    "month_of",
]
