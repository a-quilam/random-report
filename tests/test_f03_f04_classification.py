"""F03/F04 — economic classes: subsidy vs compensation, transfers, refunds, financing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.finance import build_ledger, compute_totals  # noqa: E402
from randreport.domain.flows import AccountScope, EconomicClass  # noqa: E402
from randreport.domain.timeframes import month_period  # noqa: E402
from tests import fixtures as fx  # noqa: E402


class ClassificationTestCase(unittest.TestCase):
    PERIOD = month_period("2025-07")

    def ledger(self, movements, *, policy=None, source_data_present=True):
        return build_ledger(
            movements,
            policy or fx.policy(),
            self.PERIOD,
            source_data_present=source_data_present,
        )

    def classes(self, ledger, movement_id):
        return [c for c in ledger.classifications if c.movement_id == movement_id]


class TestF03ContourBoundary(ClassificationTestCase):
    def test_contribution_and_compensation_are_separate(self):
        movements = [
            fx.income("m_donations", "2025-07-02", 50000, "cat_donations"),
            fx.income("m_subsidy", "2025-07-06", 40000, "cat_commune_subsidy"),
            fx.expense("m_rent", "2025-07-01", 30000, "cat_rent"),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertEqual(50000, totals.compensation_kopecks)
        self.assertEqual(40000, totals.subsidy_kopecks)
        self.assertEqual(EconomicClass.EXTERNAL_COMPENSATION, self.classes(ledger, "m_donations")[0].economic_class)
        self.assertEqual(EconomicClass.COMMUNE_SUBSIDY, self.classes(ledger, "m_subsidy")[0].economic_class)

    def test_direct_payment_and_reimbursement_are_not_double_counted(self):
        movements = [
            fx.expense("m_purchase", "2025-07-02", 30000, "cat_equipment"),
            fx.income(
                "m_reimbursement",
                "2025-07-09",
                30000,
                "cat_commune_direct_pay",
                pair_id="pair_direct",
                pair_kind="reimbursement",
                reimbursement_of="m_purchase",
            ),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertEqual(30000, totals.expense_kopecks)
        self.assertEqual(30000, totals.subsidy_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)
        expense_rows = [c for c in ledger.classifications if c.economic_class is EconomicClass.OPERATING_EXPENSE]
        self.assertEqual(1, len(expense_rows))
        self.assertIn("pair_direct", ledger.reimbursements)
        self.assertNotIn("pair_direct", ledger.internal_transfer_pairs)

    def test_mixed_living_public_category_stays_in_quarantine(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 20000, "cat_rent"),
            fx.expense("m_shared", "2025-07-04", 77777, "cat_flat_shared"),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertIn("m_shared", ledger.quarantined_ids)
        self.assertEqual(20000, totals.expense_kopecks)
        self.assertEqual("partial", totals.completeness)
        self.assertIn("quarantine_present", totals.labels)
        amounts = [row.amount_kopecks for row in totals.categories]
        self.assertNotIn(77777, amounts)

    def test_quarantine_is_not_silently_added_to_unclassified(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 20000, "cat_rent"),
            fx.expense("m_shared", "2025-07-04", 77777, "cat_flat_shared"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(0, totals.unclassified_out_kopecks)
        self.assertEqual(["m_shared"], list(self.ledger(movements).quarantined_ids))

    def test_new_account_is_not_included_automatically(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 20000, "cat_rent"),
            fx.expense("m_new_account", "2025-07-05", 90000, "cat_rent", account="acc_unknown_new"),
        ]
        ledger = self.ledger(movements)
        self.assertIn("m_new_account", ledger.quarantined_ids)
        self.assertEqual(20000, compute_totals(ledger).expense_kopecks)
        self.assertIs(AccountScope.UNKNOWN, ledger.policy.account_scope("acc_unknown_new"))


class TestF04FlowKinds(ClassificationTestCase):
    def test_internal_transfer_two_legs_collapse_to_one_record(self):
        movements = [
            fx.movement("m_out", "2025-07-02", 500000, source="acc_cash", pair_id="pair_transfer", pair_kind="transfer_leg"),
            fx.movement("m_in", "2025-07-02", 500000, target="acc_savings", pair_id="pair_transfer", pair_kind="transfer_leg"),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertEqual(1, len(ledger.internal_transfer_pairs))
        self.assertIn("pair_transfer", ledger.duplicated_legs)
        self.assertEqual(0, totals.expense_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)
        self.assertEqual(0, totals.category_sum_kopecks)

    def test_cross_currency_transfer_keeps_currencies_apart(self):
        movements = [
            fx.movement("m_out", "2025-07-02", 400000, source="acc_cash", pair_id="pair_fx", pair_kind="transfer_leg"),
            fx.movement(
                "m_in",
                "2025-07-02",
                400000,
                target="acc_savings",
                currency="USD",
                pair_id="pair_fx",
                pair_kind="transfer_leg",
            ),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertIn("pair_fx", ledger.internal_transfer_pairs)
        self.assertIn("cross_currency", ledger.transfer_notes["pair_fx"])
        self.assertEqual(0, totals.expense_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)

    def test_accounts_outside_scope_are_excluded_entirely(self):
        movements = [
            fx.expense("m_private", "2025-07-02", 90000, "cat_rent", account="acc_personal"),
            fx.expense("m_rent", "2025-07-03", 10000, "cat_rent"),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertIn("m_private", ledger.excluded_ids)
        self.assertEqual(10000, totals.expense_kopecks)
        self.assertNotIn(90000, [row.amount_kopecks for row in totals.categories])

    def test_commission_is_a_separate_expense(self):
        movements = [
            fx.movement("m_out", "2025-07-02", 100000, source="acc_cash", pair_id="pair_fee", pair_kind="transfer_leg"),
            fx.movement("m_in", "2025-07-02", 100000, target="acc_savings", pair_id="pair_fee", pair_kind="transfer_leg"),
            fx.expense("m_fee", "2025-07-02", 1500, "cat_commission"),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertEqual(1500, totals.expense_kopecks)
        self.assertEqual(1, len(ledger.internal_transfer_pairs))
        self.assertEqual(EconomicClass.OPERATING_EXPENSE, self.classes(ledger, "m_fee")[0].economic_class)

    def test_purchase_refund_reduces_the_expense(self):
        movements = [
            fx.expense("m_buy", "2025-07-02", 50000, "cat_equipment"),
            fx.income("m_refund", "2025-07-06", 20000, "cat_purchase_refund"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(50000, totals.gross_expense_kopecks)
        self.assertEqual(20000, totals.expense_refund_kopecks)
        self.assertEqual(30000, totals.expense_kopecks)

    def test_donation_cancellation_reduces_the_compensation(self):
        movements = [
            fx.income("m_donations", "2025-07-02", 60000, "cat_donations"),
            fx.expense("m_cancel", "2025-07-08", 10000, "cat_donation_refund"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(60000, totals.external_compensation_kopecks)
        self.assertEqual(10000, totals.compensation_refund_kopecks)
        self.assertEqual(50000, totals.compensation_kopecks)

    def test_loans_and_repayments_are_financing_not_operating(self):
        movements = [
            fx.expense("m_rent", "2025-07-02", 10000, "cat_rent"),
            fx.income("m_loan", "2025-07-03", 200000, "cat_loan_in"),
            fx.expense("m_repay", "2025-07-20", 200000, "cat_loan_repay"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(10000, totals.expense_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)
        self.assertEqual(200000, totals.financing_in_kopecks)
        self.assertEqual(200000, totals.financing_out_kopecks)
        self.assertEqual(-10000, totals.result_before_subsidy_kopecks)
        self.assertIn("financing_present", totals.labels)

    def test_balance_adjustment_is_a_separate_flow(self):
        movements = [
            fx.expense("m_rent", "2025-07-02", 10000, "cat_rent"),
            fx.income("m_adjust", "2025-07-05", 5000, "cat_balance_adjustment"),
        ]
        totals = compute_totals(self.ledger(movements))
        self.assertEqual(5000, totals.balance_adjustment_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)
        self.assertEqual(10000, totals.expense_kopecks)

    def test_planned_movement_is_not_counted(self):
        movements = [
            fx.expense("m_planned", "2025-07-02", 40000, "cat_rent", is_planned=True),
            fx.expense("m_rent", "2025-07-03", 10000, "cat_rent"),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertIn("m_planned", ledger.planned_ids)
        self.assertEqual(10000, totals.expense_kopecks)

    def test_deleted_movement_is_not_counted_but_revision_is_kept(self):
        movements = [
            fx.expense("m_gone", "2025-07-02", 40000, "cat_rent", deleted=True),
            fx.expense("m_rent", "2025-07-03", 10000, "cat_rent"),
        ]
        ledger = self.ledger(movements)
        totals = compute_totals(ledger)
        self.assertIn("m_gone", ledger.deleted_ids)
        self.assertEqual(10000, totals.expense_kopecks)


if __name__ == "__main__":
    unittest.main()
