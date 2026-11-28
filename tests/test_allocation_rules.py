"""Owner allocation overrides: the versioned rules of the basement's share.

Covers the card's clauses, each as its own test:

* an excluded category leaves no trace in any total;
* a third of common expenses and utilities, aggregated by month in whole
  kopecks with a stated rounding, and a year that is the sum of its months;
* the agreed 90 000 ₽ rent replaces the bank payment (no double count, no
  retroactive rewrite, no invented rent for a month without data);
* the agreed and the bank figures are shown apart;
* corrections of both signs kept separately, an undefined article without an
  invented class;
* a supercategory never doubles its children;
* the JSON, the CSV and the HTML tell the same numbers;
* the strict DTOs stay closed and the policy change never rewrites a balance.

All data is synthetic; no source, secret or private file is involved.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _digits(text: object) -> str:
    """The whole-kopeck digits of a rendered amount, whatever its format."""

    return "".join(ch for ch in str(text) if ch.isdigit())

from randreport.domain.allocation import (  # noqa: E402
    AllocationError,
    AllocationPolicy,
    Correction,
    RentRule,
    ShareRule,
    allocation_policy_from_payload,
    apply_allocation,
)
from randreport.domain.dto import AllocationDTO, InvalidFieldError, MoneyDTO, UnknownFieldError  # noqa: E402
from randreport.domain.finance import (  # noqa: E402
    AccountBalance,
    build_ledger,
    category_public_key,
    compute_totals,
    consolidated_balance,
)
from randreport.domain.flows import EconomicClass  # noqa: E402
from randreport.domain.timeframes import month_period, range_period, year_period  # noqa: E402
from randreport.projection.csv_export import export_categories_csv  # noqa: E402
from randreport.projection.privacy import find_leaks  # noqa: E402
from randreport.projection.release import ReleaseContext, build_money_payload  # noqa: E402
from randreport.web import hints as web_hints  # noqa: E402
from randreport.web import render as web_render  # noqa: E402
from tests import fixtures as fx  # noqa: E402


def ctx(period=None, **overrides) -> ReleaseContext:
    base = dict(
        release_id="rel_allocation",
        as_of=fx.msk(2026, 1, 31, 3, 30),
        period=period or month_period("2026-01"),
        freshness="fresh",
        verification="operational",
        completeness="complete",
        source_labels=("zenmoney",),
    )
    base.update(overrides)
    return ReleaseContext(**base)


def allocate(movements, policy, period, allocation):
    ledger = build_ledger(movements, policy, period)
    allocated, report = apply_allocation(ledger, allocation, policy=policy)
    return compute_totals(allocated), report


class ExcludedCategoryTests(unittest.TestCase):
    def test_an_excluded_category_leaves_no_trace_in_any_total(self):
        policy = fx.policy()
        movements = [
            fx.expense("m_eq", "2026-01-05", 100_000, "cat_equipment"),
            fx.expense("m_rent", "2026-01-06", 4_000_000, "cat_rent"),
        ]
        baseline, _ = allocate(movements, policy, month_period("2026-01"), AllocationPolicy())
        self.assertEqual(4_100_000, baseline.expense_kopecks)

        allocation = AllocationPolicy(
            version="alloc-1", excluded_category_ids=frozenset({"cat_equipment"})
        )
        totals, report = allocate(movements, policy, month_period("2026-01"), allocation)
        self.assertEqual(4_000_000, totals.expense_kopecks)
        self.assertEqual(4_000_000, totals.gross_expense_kopecks)
        self.assertEqual(4_000_000, totals.category_sum_kopecks)
        keys = {row.key for row in totals.categories}
        self.assertNotIn(category_public_key("cat_equipment"), keys)
        self.assertEqual((category_public_key("cat_equipment"),), report.excluded_categories)
        self.assertIn("excluded_category_present", report.labels)


class ShareOfSharedExpensesTests(unittest.TestCase):
    """One third of common expenses and utilities, aggregated by whole month."""

    def _movements(self):
        return [
            # 2026-01: 100001 kopecks -> /3 = 33333.67 -> half_up to 33334
            fx.expense("u_jan_a", "2026-01-10", 60_001, "cat_utilities"),
            fx.expense("u_jan_b", "2026-01-20", 40_000, "cat_utilities"),
            # 2026-02: another 100001 -> 33334
            fx.expense("u_feb", "2026-02-14", 100_001, "cat_utilities"),
        ]

    def test_the_fraction_is_applied_to_each_month_not_to_the_year(self):
        policy = fx.policy()
        allocation = AllocationPolicy(
            version="alloc-share",
            share_rules={"cat_utilities": ShareRule(numerator=1, denominator=3, rounding="half_up")},
        )
        totals, report = allocate(self._movements(), policy, range_period("2026-01", "2026-02"), allocation)
        # 33334 + 33334 = 66668, whereas one third of the yearly 200002 is 66667
        self.assertEqual(66_668, totals.expense_kopecks)
        self.assertEqual(200_002, report.shared_expenses_before_kopecks)
        self.assertEqual(66_668, report.shared_expenses_after_kopecks)
        self.assertEqual({"share"}, {item.kind for item in report.adjustments})

    def test_the_year_is_the_sum_of_its_rounded_months(self):
        policy = fx.policy()
        allocation = AllocationPolicy(
            share_rules={"cat_utilities": ShareRule(numerator=1, denominator=3, rounding="half_up")}
        )
        yearly, _ = allocate(self._movements(), policy, year_period("2026"), allocation)
        jan, _ = allocate(self._movements(), policy, month_period("2026-01"), allocation)
        feb, _ = allocate(self._movements(), policy, month_period("2026-02"), allocation)
        self.assertEqual(yearly.expense_kopecks, (jan.expense_kopecks or 0) + (feb.expense_kopecks or 0))
        self.assertEqual(66_668, yearly.expense_kopecks)

    def test_the_rounding_rule_is_explicit_and_changeable(self):
        policy = fx.policy()
        movements = [fx.expense("u", "2026-01-10", 100_000, "cat_utilities")]  # /3 = 33333.33
        floor = AllocationPolicy(
            share_rules={"cat_utilities": ShareRule(1, 3, rounding="floor")}
        )
        ceil = AllocationPolicy(share_rules={"cat_utilities": ShareRule(1, 3, rounding="ceil")})
        down, _ = allocate(movements, policy, month_period("2026-01"), floor)
        up, _ = allocate(movements, policy, month_period("2026-01"), ceil)
        self.assertEqual(33_333, down.expense_kopecks)
        self.assertEqual(33_334, up.expense_kopecks)

    def test_an_unknown_rounding_is_refused(self):
        with self.assertRaises(AllocationError):
            ShareRule(numerator=1, denominator=3, rounding="banker-ish")


class RentTests(unittest.TestCase):
    AGREED = 9_000_000  # 90 000.00 ₽

    def _movements(self):
        return [
            fx.expense("r_jan", "2026-01-03", 4_000_000, "cat_rent"),
            fx.expense("r_feb", "2026-02-03", 4_500_000, "cat_rent"),
            fx.expense("u_jan", "2026-01-09", 300_000, "cat_utilities"),
        ]

    def _allocation(self, **kwargs):
        return AllocationPolicy(
            version="alloc-rent",
            rent_category_ids=frozenset({"cat_rent"}),
            rent_rule=RentRule(monthly_kopecks=self.AGREED, **kwargs),
        )

    def test_the_agreed_amount_replaces_the_payment_without_double_counting(self):
        policy = fx.policy()
        totals, report = allocate(
            self._movements(), policy, range_period("2026-01", "2026-02"), self._allocation()
        )
        # 9 000 000 (Jan) + 9 000 000 (Feb) + 300 000 utilities; the 8 500 000 the
        # bank actually took is *reported*, never added to the expense total
        self.assertEqual(18_300_000, totals.expense_kopecks)
        self.assertEqual(18_000_000, report.computed_rent_kopecks)
        self.assertEqual(8_500_000, report.bank_rent_kopecks)
        self.assertTrue(report.rent_differs)
        self.assertTrue(report.rent_applied)

    def test_the_agreed_rent_and_the_bank_payment_are_distinct_numbers(self):
        policy = fx.policy()
        _, report = allocate(
            self._movements(), policy, range_period("2026-01", "2026-02"), self._allocation()
        )
        public = report.to_public()
        self.assertEqual(18_000_000, public["computed_rent_kopecks"])
        self.assertEqual(8_500_000, public["bank_rent_kopecks"])
        months = {item["month"]: item for item in public["rent_months"]}
        self.assertEqual(9_000_000, months["2026-01"]["computed_kopecks"])
        self.assertEqual(4_000_000, months["2026-01"]["bank_kopecks"])

    def test_a_month_before_the_rule_keeps_the_actual_payment(self):
        policy = fx.policy()
        allocation = self._allocation(from_month="2026-02")
        totals, report = allocate(
            self._movements(), policy, range_period("2026-01", "2026-02"), allocation
        )
        # January keeps its real 4 000 000, February gets the agreed 9 000 000
        self.assertEqual(4_000_000 + 9_000_000 + 300_000, totals.expense_kopecks)
        by_month = {item.month: item for item in report.rent_months}
        self.assertFalse(by_month["2026-01"].applied)
        self.assertEqual(4_000_000, by_month["2026-01"].bank_kopecks)
        self.assertTrue(by_month["2026-02"].applied)
        self.assertEqual(9_000_000, report.computed_rent_kopecks)

    def test_a_month_without_any_rent_is_not_given_an_invented_amount(self):
        policy = fx.policy()
        movements = [fx.expense("r_jan", "2026-01-03", 4_000_000, "cat_rent")]
        totals, report = allocate(
            movements, policy, range_period("2026-01", "2026-03"), self._allocation()
        )
        self.assertEqual(9_000_000, totals.expense_kopecks)
        self.assertEqual(("2026-01",), tuple(item.month for item in report.rent_months))


class CorrectionTests(unittest.TestCase):
    def test_both_signs_are_kept_separately_and_an_unknown_article_stays_unclassified(self):
        policy = fx.policy()
        movements = [fx.expense("m_eq", "2026-01-05", 100_000, "cat_equipment")]
        allocation = AllocationPolicy(
            version="alloc-corr",
            corrections=(
                Correction(period="2026-01", amount_kopecks=5_000, category_id="cat_equipment"),
                Correction(period="2026-01", amount_kopecks=-2_000, category_id="cat_equipment"),
                Correction(period="2026-01", amount_kopecks=300, category_id="cat_not_mapped"),
            ),
        )
        totals, report = allocate(movements, policy, month_period("2026-01"), allocation)
        self.assertEqual(5_300, report.corrections_positive_kopecks)
        self.assertEqual(-2_000, report.corrections_negative_kopecks)
        self.assertTrue(report.corrections_unclassified)
        self.assertIn("correction_unclassified_article", report.labels)
        # 100000 + 5000 - 2000 = 103000 classified, the undefined 300 stays unclassified
        self.assertEqual(103_000, totals.expense_kopecks)
        self.assertEqual(300, totals.unclassified_in_kopecks)
        self.assertEqual(0, totals.unclassified_out_kopecks)

    def test_an_unclassified_correction_gets_no_invented_class(self):
        policy = fx.policy()
        allocation = AllocationPolicy(
            corrections=(Correction(period="2026-01", amount_kopecks=1_000, category_id=None),)
        )
        totals, _ = allocate([], policy, month_period("2026-01"), allocation)
        self.assertEqual(1_000, totals.unclassified_in_kopecks)

    def test_a_zero_correction_is_refused(self):
        with self.assertRaises(AllocationError):
            Correction(period="2026-01", amount_kopecks=0)

    def test_the_operators_free_text_reason_is_never_published(self):
        policy = fx.policy()
        allocation = AllocationPolicy(
            corrections=(
                Correction(
                    period="2026-01",
                    amount_kopecks=100,
                    category_id="cat_rent",
                    reason="внутренняя заметка владельца",
                ),
            )
        )
        totals, report = allocate([], policy, month_period("2026-01"), allocation)
        payload = build_money_payload(ctx(), totals, allocation=report)
        text = json.dumps(payload.to_public_dict(), ensure_ascii=False)
        self.assertNotIn("внутренняя заметка владельца", text)
        self.assertEqual(100, report.corrections_positive_kopecks)


class SupercategoryTests(unittest.TestCase):
    def test_a_supercategory_does_not_double_its_children(self):
        policy = fx.policy()
        movements = [
            fx.expense("p", "2026-01-05", 1_000_000, "cat_event_costs"),
            fx.expense("c1", "2026-01-06", 400_000, "cat_supplies"),
            fx.expense("c2", "2026-01-07", 600_000, "cat_equipment"),
        ]
        naive, _ = allocate(movements, policy, month_period("2026-01"), AllocationPolicy())
        self.assertEqual(2_000_000, naive.expense_kopecks)

        allocation = AllocationPolicy(
            supercategories={"cat_event_costs": frozenset({"cat_supplies", "cat_equipment"})}
        )
        totals, report = allocate(movements, policy, month_period("2026-01"), allocation)
        self.assertEqual(1_000_000, totals.expense_kopecks)
        self.assertEqual({"supercategory"}, {item.kind for item in report.adjustments})

    def test_a_parent_carries_only_the_part_its_children_do_not_cover(self):
        policy = fx.policy()
        movements = [
            fx.expense("p", "2026-01-05", 1_000_000, "cat_event_costs"),
            fx.expense("c1", "2026-01-06", 300_000, "cat_supplies"),
        ]
        allocation = AllocationPolicy(supercategories={"cat_event_costs": frozenset({"cat_supplies"})})
        totals, _ = allocate(movements, policy, month_period("2026-01"), allocation)
        self.assertEqual(1_000_000, totals.expense_kopecks)


class PolicyPayloadTests(unittest.TestCase):
    def test_a_payload_round_trips_through_the_parser(self):
        payload = {
            "version": "owner-1",
            "excluded_category_ids": ["cat_equipment"],
            "share_rules": {"cat_utilities": {"numerator": 1, "denominator": 3, "rounding": "half_up"}},
            "rent": {"monthly_kopecks": 9_000_000, "from_month": "2026-02"},
            "rent_category_ids": ["cat_rent"],
            "supercategories": {"cat_event_costs": ["cat_supplies"]},
            "corrections": [{"period": "2026-01", "amount_kopecks": 500, "category_id": "cat_rent"}],
        }
        policy = allocation_policy_from_payload("owner-1", payload)
        self.assertEqual("owner-1", policy.version)
        self.assertEqual(frozenset({"cat_equipment"}), policy.excluded_category_ids)
        self.assertEqual(ShareRule(1, 3), policy.share_rules["cat_utilities"])
        self.assertEqual(RentRule(9_000_000, "2026-02"), policy.rent_rule)
        self.assertEqual(500, policy.corrections[0].amount_kopecks)

    def test_a_float_and_a_bad_month_are_refused(self):
        with self.assertRaises(AllocationError):
            allocation_policy_from_payload("x", {"rent": {"monthly_kopecks": 9_000_000.5}})
        with self.assertRaises(AllocationError):
            allocation_policy_from_payload("x", {"rent": {"monthly_kopecks": 9_000_000, "from_month": "February"}})
        with self.assertRaises(AllocationError):
            allocation_policy_from_payload("x", {"share_rules": {"cat_rent": {"numerator": 1}}})

    def test_an_empty_payload_is_an_inert_policy(self):
        policy = allocation_policy_from_payload("allocation", {})
        self.assertTrue(policy.is_empty)
        self.assertTrue(policy.confirmed)


class PublicConsistencyTests(unittest.TestCase):
    def _payload(self):
        policy = fx.policy()
        movements = [
            fx.expense("m_rent", "2026-01-03", 4_000_000, "cat_rent"),
            fx.expense("m_util", "2026-01-09", 300_000, "cat_utilities"),
            fx.income("m_don", "2026-01-15", 2_000_000, "cat_donations"),
        ]
        allocation = AllocationPolicy(
            version="alloc-public",
            share_rules={"cat_utilities": ShareRule(1, 3)},
            rent_category_ids=frozenset({"cat_rent"}),
            rent_rule=RentRule(9_000_000),
        )
        totals, report = allocate(movements, policy, month_period("2026-01"), allocation)
        return build_money_payload(ctx(), totals, allocation=report), report

    def test_json_csv_and_html_state_the_same_amounts(self):
        payload, _ = self._payload()
        csv_text = export_categories_csv(payload, params={"period": "2026-01"})
        html = web_render.money_section(payload, period_label=payload.period.requested, historical={})
        for category in payload.categories:
            self.assertIn(category.amount_rub, csv_text)
            # the leaflet renders money in the Russian format ("20 000,00 ₽"), so
            # the same whole kopecks must reach JSON, CSV and HTML — compare the
            # *values*, not one view's number formatting
            shown = web_render.rub_from_kopecks(category.amount_kopecks)
            self.assertIn(shown, html)
            self.assertEqual(_digits(category.amount_rub), _digits(shown))
        # the agreed rent (90 000 ₽) is what the expense total carries, and the
        # bank payment (40 000 ₽) is reported apart, never added in
        self.assertEqual(9_100_000, payload.metrics["expense_kopecks"])
        self.assertEqual(9_000_000, payload.metrics["rent_computed_kopecks"])
        self.assertEqual(4_000_000, payload.metrics["rent_bank_kopecks"])
        self.assertIn("rent_bank_payment_differs", payload.metrics["status_labels"])

    def test_the_allocation_explanation_is_a_safe_dto(self):
        payload, _ = self._payload()
        data = payload.to_public_dict()
        self.assertEqual(payload, MoneyDTO.from_public_dict(data))
        self.assertEqual((), find_leaks(data))
        self.assertEqual(9_000_000, data["allocation"]["computed_rent_kopecks"])
        self.assertEqual(1, data["allocation"]["shared_expenses_after_kopecks"] // 100_000)
        data["allocation"]["excluded_category_ids"] = ["cat_rent"]
        with self.assertRaises(UnknownFieldError):
            MoneyDTO.from_public_dict(data)
        data = payload.to_public_dict()
        data["allocation"]["computed_rent_kopecks"] = 9_000_000.5
        with self.assertRaises(InvalidFieldError):
            MoneyDTO.from_public_dict(data)

    def test_an_empty_allocation_is_an_inert_allocation_dto(self):
        dto = AllocationDTO()
        self.assertFalse(dto.applied)
        self.assertEqual(0, dto.computed_rent_kopecks)


class HintAccessibilityTests(unittest.TestCase):
    def test_an_approved_hint_is_reachable_by_touch_and_keyboard(self):
        policy = fx.policy(category_hints={"cat_rent": "Аренда подвала по согласованной ставке"})
        movements = [fx.expense("m_rent", "2026-01-03", 9_000_000, "cat_rent")]
        ledger = build_ledger(movements, policy, month_period("2026-01"))
        payload = build_money_payload(ctx(), compute_totals(ledger))
        rent = next(row for row in payload.categories if row.hint)
        self.assertEqual("Аренда подвала по согласованной ставке", rent.hint)

        disclosure = web_hints.hint_disclosure(rent.hint, label=rent.label)
        # a native disclosure: focusable and tappable without any script
        self.assertIn("<details", disclosure)
        self.assertIn("<summary", disclosure)
        self.assertNotIn("<script", disclosure)
        self.assertNotIn("onclick", disclosure)
        html = web_render.money_section(payload, period_label="2026-01", historical={})
        self.assertIn("Аренда подвала по согласованной ставке", html)
        self.assertIn("<details", html)

    def test_a_category_without_an_approved_hint_gets_no_invented_hint(self):
        policy = fx.policy()
        movement = fx.expense("m_rent", "2026-01-03", 9_000_000, "cat_rent")
        ledger = build_ledger([movement], policy, month_period("2026-01"))
        payload = build_money_payload(ctx(), compute_totals(ledger))
        self.assertIsNone(payload.categories[0].hint)
        self.assertEqual("", web_hints.hint_disclosure(None, label="Аренда"))


class BalanceTests(unittest.TestCase):
    def test_a_policy_change_never_rewrites_a_balance(self):
        balances = [
            AccountBalance("acc_card", "RUB", 123_456, fx.as_day("2026-01-31")),
            AccountBalance("acc_cash", "RUB", 7_000, fx.as_day("2026-01-31")),
        ]
        before = consolidated_balance(balances, as_of=fx.as_day("2026-01-31"))
        policy = fx.policy(
            allocation=AllocationPolicy(excluded_category_ids=frozenset({"cat_rent"}))
        )
        after = consolidated_balance(balances, as_of=fx.as_day("2026-01-31"), scope_policy=policy)
        self.assertEqual(before.kopecks, after.kopecks)
        self.assertEqual(130_456, after.kopecks)


class UnclassifiedArticleTests(unittest.TestCase):
    def test_a_shared_rule_on_a_missing_category_changes_nothing(self):
        policy = fx.policy()
        movements = [fx.expense("m", "2026-01-03", 100_000, "cat_equipment")]
        allocation = AllocationPolicy(share_rules={"cat_not_present": ShareRule(1, 3)})
        totals, report = allocate(movements, policy, month_period("2026-01"), allocation)
        self.assertEqual(100_000, totals.expense_kopecks)
        self.assertEqual(0, report.shared_expenses_before_kopecks)
        self.assertFalse(report.applied)


if __name__ == "__main__":
    unittest.main()
