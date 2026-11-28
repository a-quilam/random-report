"""Property/invariant tests for financial integrity (docs/REQUIREMENTS.md section 13).

These are parameterised characterization tests over the formulas implemented in
``randreport.domain.finance``: for every generated combination of expense /
compensation / subsidy inputs the reported values must satisfy the algebraic
identities and the null/label policy of the requirements.
"""

from __future__ import annotations

import itertools
import random
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.finance import build_ledger, compute_totals  # noqa: E402
from randreport.domain.timeframes import month_period  # noqa: E402
from tests import fixtures as fx  # noqa: E402

PERIOD = month_period("2025-07")

CASES = [
    (expense, compensation, subsidy)
    for expense, compensation, subsidy in itertools.product(
        (0, 1, 12_345, 100_000, 999_999), (0, 1, 60_000, 150_000), (0, 40_000)
    )
]


def ledger_for(expense: int, compensation: int, subsidy: int, *, subsidy_known: bool = True):
    movements = []
    if expense:
        movements.append(fx.expense("m_expense", "2025-07-02", expense, "cat_rent"))
    if compensation:
        movements.append(fx.income("m_compensation", "2025-07-03", compensation, "cat_donations"))
    if subsidy:
        movements.append(fx.income("m_subsidy", "2025-07-04", subsidy, "cat_commune_subsidy"))
    policy = fx.policy() if subsidy_known else fx.policy(subsidy_category_ids=frozenset())
    return build_ledger(movements, policy, PERIOD)


class TestFinancialIntegrityInvariants(unittest.TestCase):
    def test_formula_identities_hold_for_every_case(self):
        for expense, compensation, subsidy in CASES:
            with self.subTest(expense=expense, compensation=compensation, subsidy=subsidy):
                totals = compute_totals(ledger_for(expense, compensation, subsidy))
                self.assertEqual(expense, totals.expense_kopecks)
                self.assertEqual(compensation, totals.compensation_kopecks)
                self.assertEqual(compensation - expense, totals.result_before_subsidy_kopecks)
                self.assertEqual(expense - compensation, totals.uncovered_kopecks)
                self.assertEqual(max(expense - compensation, 0), totals.shortfall_kopecks)
                self.assertEqual(max(compensation - expense, 0), totals.excess_kopecks)
                self.assertEqual(
                    compensation + subsidy - expense, totals.result_after_subsidy_kopecks
                )
                if expense > 0:
                    # coverage is a ratio of the very same sums, up to one decimal
                    coverage = totals.coverage_percent
                    self.assertIsNotNone(coverage)
                    assert coverage is not None
                    delta = abs(coverage * Decimal(expense) - Decimal(compensation) * 100)
                    self.assertLess(delta, Decimal("0.05") * Decimal(expense) + 1)
                else:
                    self.assertIsNone(totals.coverage_percent)
                    self.assertIn("coverage_not_applicable", totals.labels)

    def test_unknown_subsidy_never_becomes_zero(self):
        for expense, compensation, _ in CASES:
            with self.subTest(expense=expense, compensation=compensation):
                totals = compute_totals(ledger_for(expense, compensation, 0, subsidy_known=False))
                self.assertIsNone(totals.subsidy_kopecks)
                self.assertIsNone(totals.result_after_subsidy_kopecks)
                self.assertIsNotNone(totals.result_before_subsidy_kopecks)

    def test_no_payload_value_is_a_float_and_rows_sum_to_totals(self):
        for expense, compensation, subsidy in random.Random(7).sample(CASES, 8):
            with self.subTest(expense=expense, compensation=compensation, subsidy=subsidy):
                totals = compute_totals(ledger_for(expense, compensation, subsidy))
                self.assertEqual(
                    totals.category_sum_kopecks,
                    sum(row.amount_kopecks for row in totals.categories),
                )
                for row in totals.categories:
                    self.assertIsInstance(row.amount_kopecks, int)
                    self.assertNotIsInstance(row.amount_kopecks, bool)
                    if row.share_percent is not None:
                        self.assertIsInstance(row.share_percent, Decimal)


if __name__ == "__main__":
    unittest.main()
