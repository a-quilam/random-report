"""E03/R02 — public projection privacy: closed events, allowlisted CSV, strict DTOs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.dto import (  # noqa: E402
    InvalidFieldError,
    MoneyDTO,
    UnknownFieldError,
)
from randreport.domain.events import event_stats, select_completed  # noqa: E402
from randreport.domain.finance import AccountBalance, build_ledger, compute_totals, consolidated_balance  # noqa: E402
from randreport.domain.timeframes import month_period, year_period  # noqa: E402
from randreport.projection.csv_export import CSV_HEADER, export_categories_csv  # noqa: E402
from randreport.projection.privacy import (  # noqa: E402
    PrivacyLeakError,
    assert_no_leaks,
    find_leaks,
    public_json,
)
from randreport.projection.query import (  # noqa: E402
    UnsafeQueryError,
    calendar_grid_granularity,
    resolve_period,
    validate_params,
)
from randreport.projection.release import (  # noqa: E402
    ReleaseContext,
    build_events_payload,
    build_money_payload,
)
from tests import fixtures as fx  # noqa: E402

CUTOFF = fx.msk(2025, 7, 31, 3, 0)
CLOSED_TITLE = "Закрытое: внутренняя встреча арендаторов подвала"
CLOSED_ID = "occ_closed_secret_1"
CLOSED_DAY = "2025-07-19"


def money_ledger(period=None, policy=None, subsidy=None):
    movements = [
        fx.expense("m_rent", "2025-07-01", 100000, "cat_rent"),
        fx.income("m_donations", "2025-07-03", 60000, "cat_donations"),
    ]
    if subsidy is not None:
        movements.append(fx.income("m_subsidy", "2025-07-11", subsidy, "cat_commune_subsidy"))
    return build_ledger(movements, policy or fx.policy(), period or month_period("2025-07"))


def events_selection():
    occurrences = [
        fx.occurrence("occ_open_1", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), title="Открытый вечер"),
        fx.occurrence(
            CLOSED_ID,
            fx.msk(2025, 7, 19, 18, 0),
            fx.msk(2025, 7, 19, 21, 0),
            subcalendars=("sub_closed",),
            title=CLOSED_TITLE,
        ),
    ]
    return select_completed(occurrences, mapping=fx.event_mapping(), cutoff=CUTOFF)


def ctx(period=None, **overrides) -> ReleaseContext:
    base = dict(
        release_id="rel_20250731_0001",
        as_of=fx.msk(2025, 7, 31, 3, 30),
        period=period or month_period("2025-07"),
        freshness="fresh",
        verification="operational",
        completeness="complete",
        source_labels=("zenmoney", "teamup"),
    )
    base.update(overrides)
    return ReleaseContext(**base)


class TestE03ClosedEvents(unittest.TestCase):
    def test_closed_event_details_never_reach_the_public_payload(self):
        payload = build_events_payload(ctx(), events_selection())
        text = public_json(payload)
        self.assertNotIn(CLOSED_TITLE, text)
        self.assertNotIn(CLOSED_ID, text)
        self.assertNotIn(CLOSED_DAY, text)
        self.assertNotIn("sub_closed", text)
        self.assertEqual(1, payload.event_type_aggregates["closed"])

    def test_closed_day_is_absent_from_the_public_calendar_grid(self):
        payload = build_events_payload(ctx(), events_selection())
        self.assertEqual({"2025-07-03": 1}, dict(payload.calendar_days))
        self.assertEqual("day", payload.calendar_granularity)

    def test_period_longer_than_a_month_exposes_only_monthly_aggregates(self):
        payload = build_events_payload(
            ctx(period=year_period("2025")), events_selection()
        )
        self.assertEqual({}, dict(payload.calendar_days))
        self.assertEqual("month", payload.calendar_granularity)

    def test_public_events_list_has_no_closed_entries_and_no_private_ids(self):
        payload = build_events_payload(ctx(), events_selection())
        self.assertEqual(1, len(payload.public_events))
        self.assertEqual("Открытый вечер", payload.public_events[0].title)
        self.assertEqual("2025-07-03", payload.public_events[0].on.isoformat())
        text = public_json(payload)
        self.assertNotIn("occ_open_1", text)
        stats = event_stats(events_selection(), cutoff=CUTOFF)
        self.assertEqual(1, stats.closed_total)
        self.assertEqual(2, stats.total)


class TestR02QueryAllowlist(unittest.TestCase):
    HOSTILE = (
        {"period": "2025-07-01"},
        {"period": "2025-04-05..2025-04-09"},
        {"period": "2025-07", "granularity": "day"},
        {"period": "2025-07", "account_id": "acc_card"},
        {"period": "2025-07", "occurrence_id": CLOSED_ID},
        {"period": "2025-07", "unit": "RUB_float"},
    )

    def setUp(self):
        self.payload = build_money_payload(
            ctx(), compute_totals(money_ledger(subsidy=40000))
        )

    def test_hostile_parameters_are_rejected_outright(self):
        for params in self.HOSTILE:
            with self.subTest(params=params):
                with self.assertRaises(UnsafeQueryError):
                    validate_params(params)
                with self.assertRaises(UnsafeQueryError):
                    resolve_period(params)
                with self.assertRaises(UnsafeQueryError):
                    export_categories_csv(self.payload, params=params)

    def test_supported_periods_resolve(self):
        self.assertEqual("month:2025-07", resolve_period({"period": "2025-07"}).bucket())
        self.assertEqual("year:2025", resolve_period({"period": "2025"}).bucket())
        self.assertEqual("all_time", resolve_period({"period": "all"}).bucket())
        self.assertEqual("day", calendar_grid_granularity(month_period("2025-07")))
        self.assertEqual("month", calendar_grid_granularity(year_period("2025")))

    def test_csv_stays_on_the_category_allowlist(self):
        csv_text = export_categories_csv(self.payload, params={"period": "2025-07", "unit": "RUB_kopecks"})
        lines = csv_text.strip().splitlines()
        self.assertEqual(",".join(CSV_HEADER), lines[0])
        self.assertEqual(len(self.payload.categories), len(lines) - 1)
        for forbidden in ("cat_rent", "cat_donations", "acc_card", "transaction_id", "counterparty", "default"):
            self.assertNotIn(forbidden, csv_text)
        self.assertNotIn(CLOSED_TITLE, csv_text)
        self.assertIn("Аренда", csv_text)

    def test_csv_requires_an_explicit_whole_period(self):
        with self.assertRaises(UnsafeQueryError):
            export_categories_csv(self.payload, params={})
        with self.assertRaises(UnsafeQueryError):
            export_categories_csv(self.payload, params={"period": "2025-08"})


class TestR02StrictDTO(unittest.TestCase):
    def setUp(self):
        self.totals = compute_totals(money_ledger(subsidy=40000))
        self.payload = build_money_payload(ctx(), self.totals)

    def test_round_trip_through_the_public_contract(self):
        data = self.payload.to_public_dict()
        self.assertEqual(self.payload, MoneyDTO.from_public_dict(data))

    def test_unknown_fields_are_rejected(self):
        data = self.payload.to_public_dict()
        data["account_id"] = "acc_card"
        with self.assertRaises(UnknownFieldError):
            MoneyDTO.from_public_dict(data)
        nested = self.payload.to_public_dict()
        nested["categories"][0]["transaction_id"] = "tx_1"
        with self.assertRaises(UnknownFieldError):
            MoneyDTO.from_public_dict(nested)

    def test_floats_are_rejected(self):
        data = self.payload.to_public_dict()
        data["metrics"]["expense_kopecks"] = 1000.5
        with self.assertRaises(InvalidFieldError):
            MoneyDTO.from_public_dict(data)
        with self.assertRaises(InvalidFieldError):
            public_json({"expense": 10.5})

    def test_money_metrics_are_integers_and_percent_strings(self):
        metrics = self.payload.metrics
        self.assertEqual(100000, metrics["expense_kopecks"])
        self.assertEqual("60.0", metrics["coverage_percent"])
        self.assertIsInstance(metrics["coverage_percent"], str)
        self.assertEqual(40000, metrics["subsidy_kopecks"])

    def test_unknown_subsidy_stays_null_in_the_payload(self):
        totals = compute_totals(money_ledger(policy=fx.policy(subsidy_category_ids=frozenset())))
        payload = build_money_payload(ctx(), totals)
        self.assertIsNone(payload.metrics["subsidy_kopecks"])
        self.assertNotEqual(0, payload.metrics["subsidy_kopecks"])
        self.assertIsNone(payload.metrics["result_after_subsidy_kopecks"])

    def test_small_sensitive_categories_do_not_expose_operation_counts(self):
        small = build_ledger(
            [fx.income("m_small", "2025-07-04", 500, "cat_donations")],
            fx.policy(),
            month_period("2025-07"),
        )
        payload = build_money_payload(ctx(), compute_totals(small))
        self.assertIsNone(payload.categories[0].operations_count)

    def test_balance_is_added_as_a_metric_only_when_known(self):
        summary = consolidated_balance([AccountBalance("acc_card", "RUB", 123456, ROOT_AS_OF, True, True)])
        payload = build_money_payload(ctx(), self.totals, balance=summary)
        self.assertEqual(123456, payload.metrics["balance_kopecks"])
        unknown = build_money_payload(ctx(), self.totals, balance=consolidated_balance([]))
        self.assertIsNone(unknown.metrics["balance_kopecks"])

    def test_privacy_validator_detects_an_injected_leak(self):
        probe = self.payload.to_public_dict()
        probe["metrics"]["account_id"] = "acc_card"
        self.assertTrue(find_leaks(probe))
        with self.assertRaises(PrivacyLeakError):
            assert_no_leaks(probe)
        clean = self.payload.to_public_dict()
        self.assertEqual((), find_leaks(clean))
        assert_no_leaks(clean)


ROOT_AS_OF = month_period("2025-07").end


if __name__ == "__main__":
    unittest.main()
