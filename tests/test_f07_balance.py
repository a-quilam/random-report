"""F07 — balance: liquid public accounts only, never the deficit, unknown ≠ 0."""

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
    Liability,
    build_ledger,
    compute_totals,
    consolidated_balance,
)
from randreport.domain.flows import AccountScope  # noqa: E402
from randreport.domain.timeframes import month_period  # noqa: E402
from tests import fixtures as fx  # noqa: E402

SNAPSHOT_DAY = month_period("2025-07").end


def balance(
    account_id: str,
    kopecks: int,
    *,
    currency: str = "RUB",
    liquid: bool = True,
    confirmed: bool = True,
    day=None,
    scope=AccountScope.PUBLIC,
):
    return AccountBalance(
        account_id=account_id,
        currency=currency,
        balance_kopecks=kopecks,
        as_of=day or SNAPSHOT_DAY,
        is_liquid=liquid,
        confirmed=confirmed,
        scope=scope,
    )


class TestF07Balance(unittest.TestCase):
    def test_balance_sums_only_liquid_public_confirmed_accounts(self):
        summary = consolidated_balance(
            [
                balance("acc_cash", 250000),
                balance("acc_savings", 1000000),
                balance("acc_personal", 999999, scope=AccountScope.PRIVATE),
                balance("acc_equipment_fund", 500000, liquid=False),
            ],
            liabilities=[Liability(id="liab_loan", amount_kopecks=500000, due_on=None, label="заём")],
        )
        self.assertEqual(1250000, summary.kopecks)
        self.assertEqual(("acc_cash", "acc_savings"), summary.included_accounts)
        self.assertIn("acc_personal", summary.excluded_accounts)
        self.assertIn("account_outside_scope", summary.labels)
        self.assertIn("not_liquid_excluded", summary.labels)
        self.assertEqual(500000, summary.obligations_kopecks)
        self.assertIn("liabilities_separate", summary.labels)

    def test_obligations_are_not_added_to_liquid_money(self):
        summary = consolidated_balance(
            [balance("acc_cash", 100000)],
            liabilities=[Liability(id="liab_loan", amount_kopecks=900000, due_on=None, label="заём")],
        )
        self.assertEqual(100000, summary.kopecks)

    def test_unknown_currency_without_rate_makes_the_balance_unknown(self):
        summary = consolidated_balance(
            [
                balance("acc_cash", 100000),
                balance("acc_usd", 10000, currency="USD"),
            ],
            rates=None,
        )
        self.assertIsNone(summary.kopecks)
        self.assertIn("no_rate", summary.labels)
        self.assertNotEqual(0, summary.kopecks)

    def test_known_rate_converts_without_inventing_a_value(self):
        summary = consolidated_balance(
            [
                balance("acc_cash", 100000),
                balance("acc_usd", 10000, currency="USD"),
            ],
            rates={"USD": Decimal("90.5")},
        )
        self.assertEqual(100000 + 905000, summary.kopecks)
        self.assertIn("converted", summary.labels)

    def test_unknown_historical_balance_is_null_not_zero(self):
        summary = consolidated_balance([])
        self.assertIsNone(summary.kopecks)
        self.assertEqual("unknown", summary.completeness)
        self.assertIn("unknown_historical_balance", summary.labels)

    def test_confirmed_zero_is_a_real_zero(self):
        summary = consolidated_balance([balance("acc_cash", 0)])
        self.assertEqual(0, summary.kopecks)
        self.assertNotIn("unknown_historical_balance", summary.labels)

    def test_unconfirmed_snapshot_is_not_published(self):
        summary = consolidated_balance([balance("acc_cash", 400000, confirmed=False)])
        self.assertIsNone(summary.kopecks)
        self.assertIn("unconfirmed_snapshot_excluded", summary.labels)

    def test_current_balance_is_not_used_for_a_missing_historical_snapshot(self):
        summary = consolidated_balance(
            [balance("acc_cash", 777000, day=SNAPSHOT_DAY)],
            as_of=month_period("2025-06").end,
        )
        self.assertIsNone(summary.kopecks)
        self.assertIn("unknown_historical_balance", summary.labels)

    def test_deficit_is_never_reported_as_the_balance(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 100000, "cat_rent"),
            fx.income("m_donations", "2025-07-02", 60000, "cat_donations"),
            fx.income("m_subsidy", "2025-07-03", 40000, "cat_commune_subsidy"),
        ]
        totals = compute_totals(build_ledger(movements, fx.policy(), month_period("2025-07")))
        self.assertEqual(40000, totals.uncovered_kopecks)
        summary = consolidated_balance([balance("acc_card", 123456)])
        self.assertEqual(123456, summary.kopecks)

    def test_scope_policy_classifies_accounts(self):
        policy = fx.policy()
        self.assertIs(AccountScope.PUBLIC, policy.account_scope("acc_cash"))
        self.assertIs(AccountScope.PRIVATE, policy.account_scope("acc_personal"))
        self.assertIs(AccountScope.UNKNOWN, policy.account_scope("acc_mixed"))
        self.assertIs(AccountScope.UNKNOWN, policy.account_scope("acc_brand_new"))


if __name__ == "__main__":
    unittest.main()
