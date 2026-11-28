"""F01/F02 — finance formulas: coverage, shortage, excess, labels and nulls."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.finance import (  # noqa: E402
    AccountBalance,
    build_ledger,
    compute_totals,
    consolidated_balance,
)
from randreport.domain.timeframes import month_period  # noqa: E402
from tests import fixtures as fx  # noqa: E402


class FormulaTestCase(unittest.TestCase):
    PERIOD = month_period("2025-07")

    def ledger(self, movements, *, policy=None, source_data_present=True):
        return build_ledger(
            movements,
            policy or fx.policy(),
            self.PERIOD,
            source_data_present=source_data_present,
        )


class TestF01BaseExample(FormulaTestCase):
    def setUp(self):
        self.movements = [
            fx.expense("m_rent", "2025-07-01", 60000, "cat_rent"),
            fx.expense("m_utils", "2025-07-05", 25000, "cat_utilities"),
            fx.expense("m_supplies", "2025-07-09", 15000, "cat_supplies"),
            fx.income("m_donations", "2025-07-03", 50000, "cat_donations"),
            fx.income("m_share", "2025-07-20", 10000, "cat_event_share"),
            fx.income("m_subsidy", "2025-07-11", 40000, "cat_commune_subsidy"),
        ]

    def test_formula_values_match_the_required_example(self):
        totals = compute_totals(self.ledger(self.movements))
        self.assertEqual(100000, totals.expense_kopecks)
        self.assertEqual(60000, totals.compensation_kopecks)
        self.assertEqual(40000, totals.subsidy_kopecks)
        self.assertEqual(Decimal("60.0"), totals.coverage_percent)
        self.assertEqual(40000, totals.uncovered_kopecks)
        self.assertEqual(40000, totals.shortfall_kopecks)
        self.assertEqual(0, totals.excess_kopecks)
        self.assertEqual(-40000, totals.result_before_subsidy_kopecks)
        self.assertEqual(0, totals.result_after_subsidy_kopecks)

    def test_balance_is_not_derived_from_the_deficit(self):
        totals = compute_totals(self.ledger(self.movements))
        self.assertEqual(40000, totals.uncovered_kopecks)

        without_snapshot = consolidated_balance([])
        self.assertIsNone(without_snapshot.kopecks)
        self.assertIn("unknown_historical_balance", without_snapshot.labels)

        snapshot = [
            AccountBalance(account_id="acc_card", currency="RUB", balance_kopecks=777777, as_of=ROOT_AS_OF)
        ]
        with_snapshot = consolidated_balance(snapshot)
        self.assertEqual(777777, with_snapshot.kopecks)
        self.assertNotEqual(totals.uncovered_kopecks, with_snapshot.kopecks)

    def test_amounts_never_appear_as_float(self):
        totals = compute_totals(self.ledger(self.movements))
        for field in ("expense_kopecks", "compensation_kopecks", "subsidy_kopecks", "uncovered_kopecks"):
            value = getattr(totals, field)
            self.assertIsInstance(value, int)
            self.assertNotIsInstance(value, bool)


ROOT_AS_OF = month_period("2025-07").end


class TestF02EdgeStates(FormulaTestCase):
    def test_coverage_over_100_yields_excess(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 100000, "cat_rent"),
            fx.income("m_donations", "2025-07-02", 80000, "cat_donations"),
            fx.income("m_share", "2025-07-03", 30000, "cat_event_share"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(100000, totals.expense_kopecks)
        self.assertEqual(110000, totals.compensation_kopecks)
        self.assertEqual(0, totals.subsidy_kopecks)
        self.assertEqual(Decimal("110.0"), totals.coverage_percent)
        self.assertEqual(-10000, totals.uncovered_kopecks)
        self.assertEqual(0, totals.shortfall_kopecks)
        self.assertEqual(10000, totals.excess_kopecks)
        self.assertEqual(10000, totals.result_before_subsidy_kopecks)
        self.assertEqual(10000, totals.result_after_subsidy_kopecks)

    def test_zero_expenses_makes_coverage_not_applicable(self):
        movements = [fx.income("m_donations", "2025-07-02", 50000, "cat_donations")]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(0, totals.expense_kopecks)
        self.assertIsNone(totals.coverage_percent)
        self.assertIn("coverage_not_applicable", totals.labels)
        self.assertEqual(50000, totals.excess_kopecks)
        self.assertEqual(0, totals.shortfall_kopecks)

    def test_expense_refunds_above_expenses_are_shown_with_explanation(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 10000, "cat_rent"),
            fx.income("m_refund", "2025-07-04", 15000, "cat_purchase_refund"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(-5000, totals.expense_kopecks)
        self.assertIsNone(totals.coverage_percent)
        self.assertIn("expense_refunds_exceed_expenses", totals.labels)
        self.assertEqual(10000, totals.gross_expense_kopecks)
        self.assertEqual(15000, totals.expense_refund_kopecks)

    def test_missing_subsidy_keeps_only_after_subsidy_result_unknown(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 100000, "cat_rent"),
            fx.income("m_donations", "2025-07-02", 60000, "cat_donations"),
        ]
        policy = fx.policy(subsidy_category_ids=frozenset())
        totals = compute_totals(self.ledger(movements, policy=policy))
        self.assertIsNone(totals.subsidy_kopecks)
        self.assertIsNone(totals.result_after_subsidy_kopecks)
        self.assertIn("subsidy_unknown", totals.labels)
        self.assertEqual(-40000, totals.result_before_subsidy_kopecks)
        self.assertEqual(Decimal("60.0"), totals.coverage_percent)

    def test_missing_subsidy_is_not_reported_as_zero(self):
        movements = [fx.expense("m_rent", "2025-07-01", 1000, "cat_rent")]
        totals = compute_totals(self.ledger(movements, policy=fx.policy(subsidy_category_ids=frozenset())))
        self.assertIsNone(totals.subsidy_kopecks)
        self.assertNotEqual(0, totals.subsidy_kopecks)

    def test_no_data_at_all_is_unknown_not_zero(self):
        ledger = self.ledger([], source_data_present=False)
        totals = compute_totals(ledger)
        self.assertIsNone(totals.expense_kopecks)
        self.assertIsNone(totals.compensation_kopecks)
        self.assertIsNone(totals.subsidy_kopecks)
        self.assertIsNone(totals.uncovered_kopecks)
        self.assertIsNone(totals.coverage_percent)
        self.assertIsNone(totals.shortfall_kopecks)
        self.assertIsNone(totals.excess_kopecks)
        self.assertEqual("unknown", totals.completeness)
        self.assertIn("no_data", totals.labels)

    def test_source_loaded_without_rows_is_a_confirmed_zero(self):
        totals = compute_totals(self.ledger([]))
        self.assertEqual(0, totals.expense_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)
        self.assertEqual(0, totals.subsidy_kopecks)
        self.assertIsNone(totals.coverage_percent)
        self.assertEqual("complete", totals.completeness)
        self.assertNotIn("no_data", totals.labels)

    def test_negative_net_compensation_is_labelled(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 100000, "cat_rent"),
            fx.income("m_donations", "2025-07-02", 60000, "cat_donations"),
            fx.expense("m_cancelled", "2025-07-03", 65000, "cat_donation_refund"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(-5000, totals.compensation_kopecks)
        self.assertIn("negative_net_compensation", totals.labels)
        self.assertEqual(Decimal("-5.0"), totals.coverage_percent)
        self.assertEqual(105000, totals.uncovered_kopecks)
        self.assertEqual(105000, totals.shortfall_kopecks)
        self.assertEqual(0, totals.excess_kopecks)


if __name__ == "__main__":
    unittest.main()
