"""F06 — kopeck precision, totals == category sums, weighted coverage, period by fact date."""

from __future__ import annotations

import random
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.finance import (  # noqa: E402
    build_ledger,
    compute_totals,
    weighted_annual_coverage,
)
from randreport.domain.money import format_kopecks, parse_rub, sum_kopecks  # noqa: E402
from randreport.domain.timeframes import MSK, month_period, msk_day  # noqa: E402
from tests import fixtures as fx  # noqa: E402


class TestF06Precision(unittest.TestCase):
    def test_ruble_strings_become_integer_kopecks(self):
        self.assertEqual(162969, parse_rub("1629.69"))
        self.assertEqual(162969, parse_rub("1629,69"))
        self.assertEqual(100000, parse_rub(1000))
        # half-even on the second decimal: 0.015 -> 0.02, 0.025 -> 0.02
        self.assertEqual(2, parse_rub(Decimal("0.015")))
        self.assertEqual(2, parse_rub(Decimal("0.025")))
        self.assertEqual("1629.69", format_kopecks(162969))

    def test_float_is_rejected_for_money(self):
        with self.assertRaises(TypeError):
            parse_rub(1629.69)

    def test_large_series_of_amounts_sums_exactly(self):
        rng = random.Random(20250701)
        amounts = [Decimal(rng.randint(1, 40000000)) / Decimal(100) for _ in range(500)]
        kopecks = [parse_rub(value) for value in amounts]
        self.assertEqual(int(sum(amounts) * 100), sum_kopecks(kopecks))

        movements = [
            fx.expense(f"m_{index:04d}", f"2025-07-{(index % 28) + 1:02d}", value, "cat_rent")
            for index, value in enumerate(kopecks)
        ]
        totals = compute_totals(build_ledger(movements, fx.policy(), month_period("2025-07")))
        self.assertEqual(sum_kopecks(kopecks), totals.expense_kopecks)
        self.assertIsInstance(totals.expense_kopecks, int)

    def test_category_totals_match_the_aggregates(self):
        movements = [
            fx.expense("m_rent", "2025-07-01", 60000, "cat_rent"),
            fx.expense("m_utils", "2025-07-05", 25000, "cat_utilities"),
            fx.income("m_donations", "2025-07-03", 50000, "cat_donations"),
            fx.income("m_share", "2025-07-20", 10000, "cat_event_share"),
            fx.income("m_subsidy", "2025-07-11", 40000, "cat_commune_subsidy"),
        ]
        totals = compute_totals(build_ledger(movements, fx.policy(), month_period("2025-07")))
        by_bucket: dict[str, int] = {}
        for row in totals.categories:
            by_bucket[row.bucket] = by_bucket.get(row.bucket, 0) + row.amount_kopecks
        self.assertEqual(totals.gross_expense_kopecks, by_bucket["operating_expense"])
        self.assertEqual(totals.external_compensation_kopecks, by_bucket["external_compensation"])
        self.assertEqual(totals.subsidy_kopecks, by_bucket["commune_subsidy"])
        self.assertEqual(totals.category_sum_kopecks, sum(row.amount_kopecks for row in totals.categories))

    def test_annual_coverage_is_weighted_across_months(self):
        narrow = compute_totals(
            build_ledger(
                [
                    fx.expense("m_a_rent", "2025-01-05", 1000, "cat_rent"),
                    fx.income("m_a_don", "2025-01-06", 1000, "cat_donations"),
                ],
                fx.policy(),
                month_period("2025-01"),
            )
        )
        wide = compute_totals(
            build_ledger(
                [
                    fx.expense("m_b_rent", "2025-02-05", 100000, "cat_rent"),
                    fx.income("m_b_don", "2025-02-06", 50000, "cat_donations"),
                ],
                fx.policy(),
                month_period("2025-02"),
            )
        )
        weighted = weighted_annual_coverage([narrow, wide])
        self.assertEqual(Decimal("50.5"), weighted)
        naive = (narrow.coverage_percent + wide.coverage_percent) / 2
        self.assertNotEqual(naive, weighted)
        self.assertEqual(Decimal("75.0"), naive)

    def test_period_boundaries_use_the_fact_date_in_moscow(self):
        december = compute_totals(
            build_ledger(
                [fx.expense("m_dec", "2025-12-31", 5000, "cat_rent")], fx.policy(), month_period("2025-12")
            )
        )
        january = compute_totals(
            build_ledger(
                [fx.expense("m_jan", "2026-01-01", 7000, "cat_rent")],
                fx.policy(),
                month_period("2026-01"),
            )
        )
        self.assertEqual(5000, december.expense_kopecks)
        self.assertEqual(7000, january.expense_kopecks)

        last_instant = msk_day(fx.utc(2025, 12, 31, 20, 59))  # 2025-12-31 23:59 MSK
        first_instant = msk_day(fx.utc(2025, 12, 31, 21, 0))  # 2026-01-01 00:00 MSK
        self.assertEqual(("2025-12-31", "2026-01-01"), (last_instant.isoformat(), first_instant.isoformat()))
        self.assertTrue(month_period("2025-12").contains(last_instant))
        self.assertFalse(month_period("2025-12").contains(first_instant))
        self.assertTrue(month_period("2026-01").contains(first_instant))

    def test_moscow_offset_is_not_the_host_timezone(self):
        noon = fx.msk(2025, 7, 15, 12, 0)
        self.assertEqual(3 * 3600, int(noon.utcoffset().total_seconds()))
        self.assertEqual("2025-07-15", msk_day(noon).isoformat())


if __name__ == "__main__":
    unittest.main()
