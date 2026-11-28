"""F05 — reconciliation: new categories, month/year agreement, historical conflicts."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.finance import (  # noqa: E402
    build_ledger,
    compare_historical,
    compute_totals,
    reconcile_totals,
    reconcile_year_vs_months,
    weighted_annual_coverage,
)
from randreport.domain.timeframes import month_period, range_period, year_period  # noqa: E402
from tests import fixtures as fx  # noqa: E402

# Four categories known in the mapping, annual totals summing to 162969 kopecks.
KNOWN_COMPENSATION = {
    "cat_donations": 62819,
    "cat_event_share": 72000,
    "cat_warehouse": 5750,
    "cat_bottles": 22400,
}
KNOWN_EXPENSE = {"cat_rent": 100000}
KNOWN_COMPENSATION_TOTAL = 162969
NEW_CATEGORY_ID = "cat_new_2026_unmapped"
NEW_CATEGORY_ANNUAL = 12345
YEAR = 2025


def build_year_movements(*, with_new_category: bool = True) -> list:
    movements: list = []
    for category, total in KNOWN_COMPENSATION.items():
        movements.extend(fx.monthly_movements(category, total, YEAR, prefix=f"{category}"))
    for category, total in KNOWN_EXPENSE.items():
        movements.extend(
            fx.monthly_movements(category, total, YEAR, prefix=f"{category}", direction="out")
        )
    if with_new_category:
        movements.extend(fx.monthly_movements(NEW_CATEGORY_ID, NEW_CATEGORY_ANNUAL, YEAR, prefix="new"))
    return movements


class ReconstructionTestCase(unittest.TestCase):
    def monthly_totals(self, movements):
        policy = fx.policy()
        return {
            f"{YEAR}-{month:02d}": compute_totals(
                build_ledger(movements, policy, month_period(f"{YEAR}-{month:02d}"), source_data_present=True)
            )
            for month in range(1, 13)
        }

    def year_ledger_and_totals(self, movements):
        ledger = build_ledger(movements, fx.policy(), year_period(str(YEAR)), source_data_present=True)
        return ledger, compute_totals(ledger)


class TestF05NewCategories(ReconstructionTestCase):
    def test_known_four_categories_sum_to_the_expected_annual_total(self):
        self.assertEqual(162969, sum(KNOWN_COMPENSATION.values()))
        _, totals = self.year_ledger_and_totals(build_year_movements(with_new_category=False))
        self.assertEqual(162969, totals.compensation_kopecks)

    def test_new_category_is_discovered_and_not_folded_into_compensation(self):
        ledger, totals = self.year_ledger_and_totals(build_year_movements())
        self.assertIn(NEW_CATEGORY_ID, ledger.discovered_categories)
        self.assertEqual(162969, totals.compensation_kopecks)
        self.assertEqual(NEW_CATEGORY_ANNUAL, totals.unclassified_in_kopecks)
        self.assertIn("unclassified_present", totals.labels)
        self.assertIn("unclassified", {row.bucket for row in totals.categories})

    def test_year_total_equals_sum_of_months(self):
        movements = build_year_movements()
        months = self.monthly_totals(movements)
        _, year = self.year_ledger_and_totals(movements)
        self.assertEqual(year.compensation_kopecks, sum(t.compensation_kopecks for t in months.values()))
        self.assertEqual(year.gross_expense_kopecks, sum(t.gross_expense_kopecks for t in months.values()))
        checks = reconcile_year_vs_months(months, year)
        self.assertEqual([], [c for c in checks if c.severity == "error"])

    def test_monthly_and_yearly_agree_without_changing_the_formula(self):
        movements = build_year_movements()
        months = self.monthly_totals(movements)
        year_ledger, year = self.year_ledger_and_totals(movements)
        self.assertEqual(162969, sum(t.compensation_kopecks for t in months.values()))
        self.assertEqual(162969, year.compensation_kopecks)
        self.assertEqual([], [c for c in reconcile_totals(year_ledger, year) if c.severity == "error"])

    def test_multi_month_range_is_one_period_not_five_reports(self):
        movements = build_year_movements(with_new_category=False)
        span = range_period(f"{YEAR}-04", f"{YEAR}-08")
        grouped = compute_totals(
            build_ledger(movements, fx.policy(), span, source_data_present=True)
        )
        monthly_sum = sum(
            compute_totals(
                build_ledger(movements, fx.policy(), month_period(spec), source_data_present=True)
            ).compensation_kopecks
            for spec in span.months()
        )
        self.assertEqual(monthly_sum, grouped.compensation_kopecks)
        self.assertEqual(
            ("2025-04", "2025-05", "2025-06", "2025-07", "2025-08"),
            span.months(),
        )


class TestF05HistoricalConflict(ReconstructionTestCase):
    def test_historical_error_is_detected_and_not_fixed_by_invented_data(self):
        movements = build_year_movements(with_new_category=False)
        ledger, totals = self.year_ledger_and_totals(movements)
        check = compare_historical(162000, totals.compensation_kopecks, label="published-2025")
        self.assertEqual("historical_conflict", check.code)
        self.assertEqual("warning", check.severity)
        self.assertEqual(162000, check.details["published_kopecks"])
        self.assertEqual(162969, check.details["computed_kopecks"])
        self.assertEqual(-969, check.details["difference_kopecks"])
        self.assertTrue(check.message)
        # the recomputed value stands, the original publication is preserved, nothing is invented
        self.assertEqual(162969, totals.compensation_kopecks)
        self.assertEqual(len(movements), ledger.movement_count)
        self.assertEqual([], [mid for mid in ledger.included_ids if mid.startswith("fix_")])

    def test_matching_historical_value_is_not_a_conflict(self):
        _, totals = self.year_ledger_and_totals(build_year_movements(with_new_category=False))
        check = compare_historical(162969, totals.compensation_kopecks, label="published-2025")
        self.assertEqual("historical_match", check.code)
        self.assertEqual("info", check.severity)

    def test_missing_published_value_is_reported_without_guessing(self):
        check = compare_historical(None, 162969, label="published-2025")
        self.assertEqual("historical_no_published", check.code)
        self.assertNotIn("difference_kopecks", check.details)


class TestF05WeightedAnnual(ReconstructionTestCase):
    def test_annual_coverage_is_weighted_not_averaged(self):
        movements = build_year_movements(with_new_category=False)
        _, year = self.year_ledger_and_totals(movements)
        months = self.monthly_totals(movements)
        weighted = weighted_annual_coverage([year])
        self.assertEqual(weighted, weighted_annual_coverage(list(months.values())))
        self.assertEqual((Decimal(162969) / Decimal(100000) * 100).quantize(Decimal("0.1")), weighted)


if __name__ == "__main__":
    unittest.main()
