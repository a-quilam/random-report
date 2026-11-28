"""Read-only operator reconciliation of the approved totals (docs/DOMAIN.md section 9).

This is an *operator* aid, not the nightly cycle and not the editorial desk. It
answers one question for the process owner: *do the approved money totals of a
period still reconcile* — within themselves (category rows against the same
query's aggregates, months against their year) **and** against the public
:class:`~randreport.domain.dto.MoneyDTO` that was actually published, whose only
money surface is aggregate and whose only explanation of incompleteness is the
provisionality labels.

The guarantees are the ones an operator reading a private store needs:

* it opens the store **read-only** (``mode=ro``, no migration, no write) and
  never touches a source, a release or an approved rule;
* it reuses the frozen domain functions unchanged — ``build_ledger`` ->
  ``apply_allocation`` -> ``compute_totals`` -> ``reconcile_totals`` /
  ``reconcile_year_vs_months`` — so its numbers are the builder's numbers;
* it reuses the public metric surface (``randreport.projection.release.money_metrics``)
  and the allowlist DTO reader, so the recomputed side and the published side
  speak the same public names;
* its output carries aggregates, bucket names and public category keys only:
  no transaction id, no account id, no raw category id, no operation. The
  report is checked with :func:`~randreport.projection.privacy.assert_no_leaks`
  before it is returned, exactly like a public payload.

A divergence is reported, never "fixed": the tool derives nothing and writes
nothing back.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..domain.allocation import apply_allocation
from ..domain.finance import (
    build_ledger,
    compute_totals,
    reconcile_totals,
    reconcile_year_vs_months,
)
from ..domain.timeframes import YEAR, Period, msk_day, month_period
from ..projection.policy import load_policy
from ..projection.privacy import assert_no_leaks
from ..projection.release import money_metrics
from ..store import repository as repo
from ..store.migrations import missing_tables

#: The aggregate money fields of the public ``MoneyDTO.metrics`` map that the
#: reconciliation compares field by field. They are *public* names; no private
#: field is ever read or written.
PUBLIC_METRIC_FIELDS: tuple[str, ...] = (
    "gross_expense_kopecks",
    "expense_refund_kopecks",
    "expense_kopecks",
    "external_compensation_kopecks",
    "compensation_refund_kopecks",
    "compensation_kopecks",
    "subsidy_kopecks",
    "uncovered_kopecks",
    "coverage_percent",
    "coverage_is_final",
    "shortfall_kopecks",
    "excess_kopecks",
    "result_before_subsidy_kopecks",
    "result_after_subsidy_kopecks",
    "unclassified_in_kopecks",
    "unclassified_out_kopecks",
    "financing_in_kopecks",
    "financing_out_kopecks",
    "balance_adjustment_kopecks",
    "internal_transfer_kopecks",
    "category_sum_kopecks",
    "contour_confirmed",
)


class ReconcileError(RuntimeError):
    """The private store could not be read, or could not be trusted, for a reconciliation."""


@dataclass(frozen=True)
class Divergence:
    """One aggregate disagreement: an internal check, or recomputed vs published."""

    code: str
    severity: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ReconcileReport:
    """The operator-visible result: approved totals, provisionality, divergences."""

    period: str
    contour_confirmed: bool
    completeness: str
    coverage_is_final: bool
    #: Why the figures are provisional (``status_labels`` of the public surface):
    #: ``period_incomplete``, ``unclassified_present``, ``quarantine_present``,
    #: ``contour_unconfirmed``, ``subsidy_unknown``, ``pair_amount_mismatch_present``,
    #: ``orphan_transfer_leg_present``, ``cross_currency_transfer_present``,
    #: ``financing_present`` and the allocation/balance labels.
    provisionality: tuple[str, ...]
    totals: Mapping[str, Any]
    divergences: tuple[Divergence, ...]

    def to_public(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "contour_confirmed": self.contour_confirmed,
            "completeness": self.completeness,
            "coverage_is_final": self.coverage_is_final,
            "provisionality": list(self.provisionality),
            "totals": {name: self.totals[name] for name in PUBLIC_METRIC_FIELDS if name in self.totals},
            "divergences": [divergence.to_public() for divergence in self.divergences],
        }

    @property
    def ok(self) -> bool:
        """True when nothing at ``error`` severity diverged."""

        return not any(divergence.severity == "error" for divergence in self.divergences)


def open_store(path: str | Path) -> sqlite3.Connection:
    """Open the private store strictly read-only (no migration, no write).

    ``mode=ro`` makes a write fail with :class:`sqlite3.OperationalError` instead
    of silently journaling, and the connection is never handed to
    ``apply_migrations``: a missing schema is refused, not created.
    """

    target = Path(path)
    if not target.exists():
        raise ReconcileError(f"no private store at {target}")
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    missing = missing_tables(conn)
    if missing:
        conn.close()
        raise ReconcileError(f"the store at {target} is not migrated (missing tables: {', '.join(missing)})")
    return conn


def _totals_for(policy: Any, movements: Any, period: Period, *, as_of: Any) -> tuple[Any, Any, Any]:
    """The builder's own pipeline for one period: classify, allocate, total."""

    ledger = build_ledger(movements, policy, period, source_data_present=bool(movements))
    ledger, allocation = apply_allocation(ledger, policy.allocation, policy=policy)
    totals = compute_totals(ledger, as_of=as_of)
    return ledger, totals, allocation


def _published_metrics(release_dir: str | Path, period_label: str) -> Mapping[str, Any] | None:
    """The published public metrics for a period, via the allowlist DTO reader."""

    from ..web.release_index import ReleaseIndex

    release = ReleaseIndex(release_dir).by_period(period_label)
    money = release.money if release is not None else None
    return None if money is None else money.metrics


def _compare_published(computed: Mapping[str, Any], published: Mapping[str, Any]) -> tuple[Divergence, ...]:
    """Field-by-field comparison of recomputed totals against the published DTO."""

    out: list[Divergence] = []
    for field_name in PUBLIC_METRIC_FIELDS:
        if field_name not in published:
            continue
        computed_value = computed.get(field_name)
        published_value = published[field_name]
        if computed_value != published_value:
            out.append(
                Divergence(
                    code="published_mismatch",
                    severity="warning",
                    message=f"the published {field_name} differs from the recomputed approved value",
                    details={"field": field_name, "computed": computed_value, "published": published_value},
                )
            )

    def labels(metrics: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(part for part in str(metrics.get("status_labels") or "").split(",") if part)

    computed_labels = labels(computed)
    published_labels = labels(published)
    if computed_labels != published_labels:
        out.append(
            Divergence(
                code="provisionality_mismatch",
                severity="warning",
                message="the published provisionality labels differ from the recomputed ones",
                details={"computed": list(computed_labels), "published": list(published_labels)},
            )
        )
    return tuple(out)


def reconcile(
    conn: sqlite3.Connection,
    *,
    period: Period,
    as_of: Any = None,
    release_dir: str | Path | None = None,
    policy: Any = None,
) -> ReconcileReport:
    """Reconcile the approved totals of ``period``, read-only.

    Without ``release_dir`` the report reconciles the store internally (category
    rows against the aggregates; months against a year). With it, the recomputed
    aggregate surface is also compared against the published ``MoneyDTO`` of that
    period. Nothing is derived beyond the frozen domain and nothing is written.
    """

    resolved_policy = policy if policy is not None else load_policy(conn)
    cutoff = as_of if as_of is not None else msk_day(datetime.now(timezone.utc))

    movements = repo.load_movements(conn)

    ledger, totals, allocation = _totals_for(resolved_policy, movements, period, as_of=cutoff)
    checks = list(reconcile_totals(ledger, totals))

    if period.kind == YEAR:
        months: dict[str, Any] = {}
        for spec in period.months():
            _, month_totals, _ = _totals_for(resolved_policy, movements, month_period(spec), as_of=cutoff)
            months[spec] = month_totals
        checks.extend(reconcile_year_vs_months(months, totals))

    metrics = money_metrics(totals, allocation=allocation)

    divergences = [Divergence(c.code, c.severity, c.message, c.details) for c in checks]
    if release_dir is not None:
        published = _published_metrics(release_dir, period.label)
        if published is not None:
            divergences.extend(_compare_published(metrics, published))

    provisionality = tuple(part for part in str(metrics.get("status_labels") or "").split(",") if part)
    report = ReconcileReport(
        period=period.label,
        contour_confirmed=bool(metrics.get("contour_confirmed", False)),
        completeness=totals.completeness,
        coverage_is_final=totals.coverage_is_final,
        provisionality=provisionality,
        totals=dict(metrics),
        divergences=tuple(divergences),
    )
    # the report is an outward-facing payload: check it exactly like a release
    assert_no_leaks(report.to_public())
    return report


__all__ = [
    "PUBLIC_METRIC_FIELDS",
    "Divergence",
    "ReconcileError",
    "ReconcileReport",
    "open_store",
    "reconcile",
]
