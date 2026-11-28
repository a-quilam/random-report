"""F08 — the standard CLI's finance policy, the real Zenmoney schema and periods.

The nightly CLI used to build every release with a hardcoded, empty scope policy
(``env-policy-unset``): no account and no category was confirmed, so every
movement of the real contour fell into quarantine and the public money section
presented **zeros** instead of the confirmed contour.  This module pins the
fixed behaviour:

* the confirmed account/category map is loaded from the private store (the
  editorial desk's versioned mapping, which is never overwritten) or from a
  private configuration file, and it actually drives the projection;
* with no confirmed contour the indicators are ``None`` (unknown), never zeros,
  and an unmapped category is never counted as a donation;
* the real Zenmoney schema is handled: one-account money moves (expense, income,
  refund) are not turned into transfers, a transfer keeps both of its amounts,
  a hard deletion is honoured;
* account liquidity is explicit and is *not* implied by an account's belonging
  to the public contour;
* all whole periods that have data plus the current unfinished one (and the
  weighted year) are published, and the API only ever receives read requests.

Everything runs against synthetic fixtures and a loopback fixture HTTP server;
no real credential, file, source or paid service is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.connectors.zenmoney import (  # noqa: E402
    LIQUID_ACCOUNT_TYPES,
    ZenmoneyClient,
    ZenmoneyProtocolError,
    account_balances,
)
from randreport.domain.finance import (  # noqa: E402
    ScopePolicy,
    build_ledger,
    compute_totals,
    consolidated_balance,
    reconcile_totals,
    reconcile_year_vs_months,
)
from randreport.domain.flows import AccountScope, EconomicClass  # noqa: E402
from randreport.domain.timeframes import MSK, month_period, year_period  # noqa: E402
from randreport.ops import cli as ops_cli  # noqa: E402
from randreport.projection.policy import (  # noqa: E402
    load_policy,
    policy_from_store,
    scope_policy_from_payload,
)
from randreport.projection.privacy import find_leaks  # noqa: E402
from randreport.store import apply_migrations, connect  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402

PERIOD = month_period("2025-07")

# The synthetic public contour as it would be recorded by the editorial desk.
SYNTHETIC_PAYLOAD = {
    "public_accounts": list(fx.PUBLIC_ACCOUNTS),
    "private_accounts": list(fx.PRIVATE_ACCOUNTS),
    "quarantined_accounts": list(fx.QUARANTINED_ACCOUNTS),
    "category_classes": {key: str(value) for key, value in fx.CATEGORY_CLASSES.items()},
    "category_labels": dict(fx.CATEGORY_LABELS),
    "quarantined_categories": sorted(fx.QUARANTINED_CATEGORIES),
    "commission_categories": sorted(fx.COMMISSION_CATEGORIES),
    "subsidy_category_ids": sorted(fx.SUBSIDY_CATEGORIES),
    "count_visibility_threshold_kopecks": 100000,
    "sensitive_categories": [],
    "unclassified_label": "требуют классификации",
}


def fresh_conn():
    conn = connect()
    apply_migrations(conn)
    return conn


def bucket_totals(money: dict) -> dict[str, int]:
    """Sum the amount of every public category bucket (a bucket may hold rows)."""

    totals: dict[str, int] = {}
    for row in money["categories"]:
        totals[row["bucket"]] = totals.get(row["bucket"], 0) + row["amount_kopecks"]
    return totals


# --------------------------------------------------------------------------- #
# 1. The policy is loaded from the confirmed map (store or private file)
# --------------------------------------------------------------------------- #

class TestPolicyLoading(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.addCleanup(self.conn.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="randreport-policy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_mapping_payload_becomes_a_confirmed_scope_policy(self):
        policy = scope_policy_from_payload("policy-1", SYNTHETIC_PAYLOAD)
        self.assertTrue(policy.contour_confirmed)
        self.assertEqual("policy-1", policy.version)
        self.assertIs(AccountScope.PUBLIC, policy.account_scope("acc_card"))
        self.assertIs(AccountScope.PRIVATE, policy.account_scope("acc_personal"))
        self.assertIs(AccountScope.UNKNOWN, policy.account_scope("acc_mixed"))
        self.assertIs(EconomicClass.EXTERNAL_COMPENSATION, policy.class_for("cat_donations"))
        self.assertEqual("Донаты", policy.label_for("cat_donations"))
        self.assertIsNone(policy.class_for("cat_brand_new"))

    def test_an_unknown_economic_class_in_a_payload_is_refused(self):
        broken = dict(SYNTHETIC_PAYLOAD, category_classes={"cat_rent": "profit"})
        with self.assertRaises(ValueError):
            scope_policy_from_payload("policy-broken", broken)

    def test_the_store_mapping_is_used_when_recorded(self):
        repo.record_category_mapping(self.conn, version="policy-7", payload=SYNTHETIC_PAYLOAD)
        policy = load_policy(self.conn)
        self.assertEqual("policy-7", policy.version)
        self.assertTrue(policy.contour_confirmed)
        self.assertEqual("Донаты", policy.label_for("cat_donations"))

    def test_a_private_file_is_used_when_the_store_has_no_mapping(self):
        path = self.tmp / "finance_policy.json"
        path.write_text(json.dumps(dict(SYNTHETIC_PAYLOAD, version="file-policy-1")), encoding="utf-8")
        policy = load_policy(self.conn, config_path=str(path))
        self.assertEqual("file-policy-1", policy.version)
        self.assertTrue(policy.contour_confirmed)
        self.assertIs(AccountScope.PUBLIC, policy.account_scope("acc_cash"))

    def test_the_recorded_desk_mapping_is_not_overwritten_by_the_file(self):
        repo.record_category_mapping(self.conn, version="policy-desk", payload=SYNTHETIC_PAYLOAD)
        path = self.tmp / "finance_policy.json"
        path.write_text(
            json.dumps(dict(SYNTHETIC_PAYLOAD, version="file-policy-1", public_accounts=["acc_wrong"])),
            encoding="utf-8",
        )
        policy = load_policy(self.conn, config_path=str(path))
        self.assertEqual("policy-desk", policy.version)
        self.assertIs(AccountScope.UNKNOWN, policy.account_scope("acc_wrong"))
        # and the store keeps exactly one version: loading never writes
        rows = self.conn.execute("SELECT version FROM category_mapping_versions").fetchall()
        self.assertEqual(["policy-desk"], [str(row["version"]) for row in rows])

    def test_no_confirmed_map_means_an_unconfirmed_contour(self):
        policy = load_policy(self.conn)
        self.assertFalse(policy.contour_confirmed)
        self.assertEqual("unmapped", policy.version)
        self.assertFalse(policy.public_accounts)

    def test_a_missing_private_file_is_refused_loudly(self):
        with self.assertRaises(FileNotFoundError):
            load_policy(self.conn, config_path=str(self.tmp / "absent.json"))

    def test_policy_from_store_matches_load_policy(self):
        repo.record_category_mapping(self.conn, version="policy-9", payload=SYNTHETIC_PAYLOAD)
        self.assertEqual(policy_from_store(self.conn).version, load_policy(self.conn).version)


# --------------------------------------------------------------------------- #
# 2. Incomplete contour -> nullable indicators (never zeros, never donations)
# --------------------------------------------------------------------------- #

class TestUnconfirmedContour(unittest.TestCase):
    def movements(self):
        return [
            fx.expense("m_rent", "2025-07-01", 100000, "cat_rent"),
            fx.income("m_donations", "2025-07-02", 60000, "cat_donations"),
            fx.income("m_subsidy", "2025-07-03", 40000, "cat_commune_subsidy"),
        ]

    def ledger(self, policy):
        return build_ledger(self.movements(), policy, PERIOD, source_data_present=True)

    def test_an_unconfirmed_contour_reports_unknown_not_zeros(self):
        ledger = self.ledger(ScopePolicy(version="unmapped", contour_confirmed=False))
        totals = compute_totals(ledger)
        self.assertIsNone(totals.expense_kopecks)
        self.assertIsNone(totals.compensation_kopecks)
        self.assertIsNone(totals.subsidy_kopecks)
        self.assertIsNone(totals.uncovered_kopecks)
        self.assertIsNone(totals.shortfall_kopecks)
        self.assertIsNone(totals.excess_kopecks)
        self.assertIsNone(totals.coverage_percent)
        self.assertEqual("unknown", totals.completeness)
        self.assertIn("contour_unconfirmed", totals.labels)
        self.assertNotEqual(0, totals.expense_kopecks)

    def test_an_unconfirmed_contour_publishes_no_categories(self):
        totals = compute_totals(self.ledger(ScopePolicy(version="unmapped", contour_confirmed=False)))
        self.assertEqual((), totals.categories)
        self.assertEqual((), reconcile_totals(self.ledger(ScopePolicy(version="unmapped", contour_confirmed=False)), totals))

    def test_a_confirmed_contour_still_reports_confirmed_zeros(self):
        totals = compute_totals(build_ledger([], fx.policy(), PERIOD, source_data_present=True))
        self.assertEqual(0, totals.expense_kopecks)
        self.assertEqual("complete", totals.completeness)
        self.assertNotIn("contour_unconfirmed", totals.labels)


# --------------------------------------------------------------------------- #
# 3. An unknown category is never a donation
# --------------------------------------------------------------------------- #

class TestUnknownCategoryIsNotADonation(unittest.TestCase):
    def ledger(self):
        return build_ledger(
            [
                fx.income("m_unmapped", "2025-07-04", 50000, "cat_brand_new"),
                fx.income("m_donations", "2025-07-03", 60000, "cat_donations"),
            ],
            fx.policy(),
            PERIOD,
        )

    def test_an_unmapped_income_stays_unclassified(self):
        ledger = self.ledger()
        classes = {c.movement_id: c.economic_class for c in ledger.classifications}
        self.assertIs(EconomicClass.UNCLASSIFIED, classes["m_unmapped"])
        self.assertIs(EconomicClass.EXTERNAL_COMPENSATION, classes["m_donations"])
        self.assertIn("cat_brand_new", ledger.discovered_categories)

    def test_an_unmapped_income_does_not_become_compensation(self):
        totals = compute_totals(self.ledger())
        self.assertEqual(60000, totals.compensation_kopecks)
        self.assertEqual(50000, totals.unclassified_in_kopecks)
        self.assertIn("unclassified_present", totals.labels)
        buckets = {row.bucket for row in totals.categories}
        self.assertIn("unclassified", buckets)
        unclassified = [row for row in totals.categories if row.bucket == "unclassified"]
        self.assertEqual(1, len(unclassified))
        self.assertEqual(50000, unclassified[0].amount_kopecks)
        self.assertNotIn("external_compensation", {row.bucket for row in unclassified})

    def test_many_unmapped_categories_collapse_into_the_unclassified_aggregate(self):
        """Unapproved sums are not published one row per source category.

        The card (t_1b5b08c8) says the launch must not reveal individual unknown
        operations or unapproved sums, so every unclassified category merges into
        the single anonymised bucket — split only by direction, matching the
        unclassified totals — with no per-category key or amount.
        """

        ledger = build_ledger(
            [
                fx.expense("m_a", "2025-07-04", 30000, "cat_brand_a"),
                fx.expense("m_b", "2025-07-05", 40000, "cat_brand_b"),
                fx.expense("m_c", "2025-07-06", 50000, "cat_brand_c"),
                fx.income("m_d", "2025-07-07", 70000, "cat_brand_d"),
                fx.income("m_known", "2025-07-08", 60000, "cat_donations"),
            ],
            fx.policy(),
            PERIOD,
        )
        totals = compute_totals(ledger)
        unclassified = [row for row in totals.categories if row.bucket == "unclassified"]
        # two directions, one aggregate row each — not four source categories
        self.assertEqual(2, len(unclassified))
        self.assertEqual({120000, 70000}, {row.amount_kopecks for row in unclassified})
        self.assertEqual({"k_unclassified"}, {row.key for row in unclassified})
        self.assertEqual({"требуют классификации"}, {row.label for row in unclassified})
        self.assertNotIn("cat_brand_a", {row.category_id for row in totals.categories})


# --------------------------------------------------------------------------- #
# 4. The real Zenmoney schema: money / transfer / refund / deletion
# --------------------------------------------------------------------------- #

class TestZenmoneyRealSchema(unittest.TestCase):
    """Rows shaped like the documented ``/v8/diff/`` payload (both accounts set)."""

    INSTRUMENTS = {1: "USD", 2: "RUB"}

    def client(self, *, instrument_currencies=None):
        return ZenmoneyClient(
            ig.RecordingTransport(lambda request: ig.json_response({})),
            instrument_currencies=self.INSTRUMENTS if instrument_currencies is None else instrument_currencies,
        )

    def normalize(self, rows, *, payload_extra=None, instrument_currencies=None):
        payload = ig.zen_payload(rows)
        if payload_extra:
            payload.update(payload_extra)
        return self.client(instrument_currencies=instrument_currencies).normalize(payload)

    @staticmethod
    def row(
        transaction_id,
        on,
        *,
        income=0,
        outcome=0,
        income_account="acc_card",
        outcome_account="acc_card",
        tag=None,
        deleted=False,
        held=False,
        income_instrument=2,
        outcome_instrument=2,
    ):
        return {
            "id": transaction_id,
            "date": on,
            "income": income / 100,
            "outcome": outcome / 100,
            "incomeAccount": income_account,
            "outcomeAccount": outcome_account,
            "incomeInstrument": income_instrument,
            "outcomeInstrument": outcome_instrument,
            "tag": list(tag or []),
            **({"deleted": True} if deleted else {}),
            **({"hold": True} if held else {}),
        }

    def test_a_same_account_expense_is_one_movement_not_a_transfer(self):
        diff = self.normalize([self.row("t1", "2025-07-05", outcome=50000, tag=["cat_rent"])])
        self.assertEqual(1, len(diff.movements))
        movement = diff.movements[0]
        self.assertEqual("t1", movement.id)
        self.assertEqual(50000, movement.amount_kopecks)
        self.assertEqual("acc_card", movement.source_account)
        self.assertIsNone(movement.target_account)
        self.assertIsNone(movement.pair_id)
        self.assertEqual("RUB", movement.currency)

    def test_a_same_account_income_is_one_movement(self):
        diff = self.normalize([self.row("t2", "2025-07-06", income=60000, tag=["cat_donations"])])
        self.assertEqual(1, len(diff.movements))
        movement = diff.movements[0]
        self.assertEqual(60000, movement.amount_kopecks)
        self.assertIsNone(movement.source_account)
        self.assertEqual("acc_card", movement.target_account)
        self.assertIsNone(movement.pair_id)

    def test_a_purchase_refund_is_one_income_movement(self):
        diff = self.normalize([self.row("t3", "2025-07-07", income=20000, tag=["cat_purchase_refund"])])
        self.assertEqual(1, len(diff.movements))
        self.assertEqual("cat_purchase_refund", diff.movements[0].category_id)
        self.assertIsNone(diff.movements[0].pair_kind)

    def test_a_transfer_keeps_both_amounts_and_both_currencies(self):
        diff = self.normalize(
            [
                self.row(
                    "t4",
                    "2025-07-08",
                    income=10000,
                    outcome=500000,
                    income_account="acc_savings",
                    outcome_account="acc_card",
                    income_instrument=1,
                    outcome_instrument=2,
                )
            ]
        )
        self.assertEqual(2, len(diff.movements))
        out_leg = next(m for m in diff.movements if m.source_account)
        in_leg = next(m for m in diff.movements if m.target_account)
        self.assertEqual(500000, out_leg.amount_kopecks)
        self.assertEqual("RUB", out_leg.currency)
        self.assertEqual(1, len({m.pair_id for m in diff.movements}))
        self.assertEqual("pair:t4", out_leg.pair_id)
        # the receiving wing carries the amount that really arrived, in USD
        self.assertEqual(1, len([m for m in diff.movements if m.currency == "USD"]))
        totals = compute_totals(
            build_ledger(diff.movements, fx.policy(), PERIOD, source_data_present=True)
        )
        self.assertEqual(0, totals.expense_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)
        self.assertEqual(500000, totals.internal_transfer_kopecks)

    def test_a_transfer_between_two_public_accounts_is_internal_not_income(self):
        diff = self.normalize(
            [self.row("t5", "2025-07-09", income=50000, outcome=50000, income_account="acc_savings", outcome_account="acc_card")]
        )
        ledger = build_ledger(diff.movements, fx.policy(), PERIOD)
        totals = compute_totals(ledger)
        self.assertEqual(1, len(ledger.internal_transfer_pairs))
        self.assertEqual(0, totals.compensation_kopecks)
        self.assertEqual(0, totals.expense_kopecks)

    def test_a_hard_deletion_in_the_deletion_array_is_honoured(self):
        diff = self.normalize(
            [],
            payload_extra={
                "deletion": [
                    {"id": "t9", "object": "transaction", "stamp": 1, "user": 1},
                    {"id": "acc_card", "object": "account", "stamp": 1, "user": 1},
                ]
            },
        )
        self.assertEqual(("t9",), diff.deleted_ids)

    def test_zero_amount_rows_are_still_skipped(self):
        diff = self.normalize([self.row("t0", "2025-07-05", income=0, outcome=0)])
        self.assertEqual((), diff.movements)
        self.assertEqual(1, diff.skipped_zero_amount)

    def test_a_same_account_two_sided_row_is_its_net_move(self):
        diff = self.normalize([self.row("t8", "2025-07-12", income=70000, outcome=50000, tag=["cat_donations"])])
        self.assertEqual(1, len(diff.movements))
        movement = diff.movements[0]
        self.assertEqual(20000, movement.amount_kopecks)
        self.assertIsNone(movement.source_account)
        self.assertEqual("acc_card", movement.target_account)
        self.assertIn("same_account_two_sided", movement.notes)

    def test_a_row_without_any_account_is_refused(self):
        row = self.row("t9", "2025-07-13", outcome=1000, tag=["cat_rent"])
        row.pop("incomeAccount")
        row.pop("outcomeAccount")
        with self.assertRaises(ZenmoneyProtocolError):
            self.normalize([row])

    def test_a_held_operation_is_not_an_actual_movement_yet(self):
        diff = self.normalize([self.row("t7", "2025-07-11", outcome=90000, tag=["cat_rent"], held=True)])
        self.assertTrue(diff.movements[0].is_planned)
        ledger = build_ledger(diff.movements, fx.policy(), PERIOD)
        self.assertEqual(0, compute_totals(ledger).expense_kopecks)
        self.assertIn("t7", ledger.planned_ids)
        # once the source settles the row (hold cleared) the same id counts
        settled = self.normalize([self.row("t7", "2025-07-11", outcome=90000, tag=["cat_rent"])])
        self.assertEqual(90000, compute_totals(build_ledger(settled.movements, fx.policy(), PERIOD)).expense_kopecks)

    def test_an_instrument_array_in_the_payload_resolves_the_currency(self):
        payload = ig.zen_payload(
            [
                self.row("t6", "2025-07-10", income=70000, tag=["cat_warehouse"], income_instrument=7),
            ]
        )
        payload["instrument"] = [{"id": 7, "title": "EUR", "shortTitle": "EUR", "symbol": "€"}]
        diff = self.client(instrument_currencies={}).normalize(payload)
        self.assertEqual("EUR", diff.movements[0].currency)
        self.assertEqual((), diff.currency_unknown)


# --------------------------------------------------------------------------- #
# 5. Liquidity is explicit, never implied by belonging to the contour
# --------------------------------------------------------------------------- #

class TestAccountLiquidity(unittest.TestCase):
    INSTRUMENTS = {2: "RUB"}

    def payload(self):
        return {
            "serverTimestamp": 1000,
            "transaction": [],
            "instrument": [{"id": 2, "title": "RUB", "shortTitle": "RUB", "symbol": "₽"}],
            "account": [
                {"id": "acc_card", "type": "ccard", "instrument": 2, "balance": 500.0},
                {"id": "acc_cash", "type": "cash", "instrument": 2, "balance": 250.0},
                # a public account whose money is real but not liquid
                {"id": "acc_savings", "type": "deposit", "instrument": 2, "balance": 9000.0},
                {"id": "acc_loan", "type": "loan", "instrument": 2, "balance": 1000.0},
                {"id": "acc_odd", "type": "crypto", "instrument": 2, "balance": 70.0},
                {"id": "acc_closed", "type": "checking", "instrument": 2, "balance": 30.0, "archive": True},
                {"id": "acc_no_snapshot", "type": "ccard", "instrument": 2},
                {"id": "acc_personal", "type": "ccard", "instrument": 2, "balance": 7777.77},
            ],
        }

    def balances(self):
        client = ZenmoneyClient(
            ig.RecordingTransport(lambda request: ig.json_response({})),
            instrument_currencies=self.INSTRUMENTS,
        )
        diff = client.normalize(self.payload())
        return account_balances(diff.account_rows, policy=fx.policy(), as_of=month_period("2025-07").end)

    def test_only_liquid_public_accounts_are_liquid(self):
        by_id = {b.account_id: b for b in self.balances()}
        self.assertTrue(by_id["acc_card"].is_liquid)
        self.assertTrue(by_id["acc_cash"].is_liquid)
        self.assertFalse(by_id["acc_savings"].is_liquid)
        self.assertFalse(by_id["acc_loan"].is_liquid)
        self.assertFalse(by_id["acc_odd"].is_liquid)
        self.assertFalse(by_id["acc_closed"].is_liquid)
        self.assertNotIn("acc_no_snapshot", by_id)

    def test_belonging_to_the_contour_does_not_make_an_account_liquid(self):
        policy = fx.policy()
        self.assertIs(AccountScope.PUBLIC, policy.account_scope("acc_savings"))
        summary = consolidated_balance(self.balances(), rates={}, scope_policy=policy)
        self.assertEqual(75000, summary.kopecks)
        self.assertIn("acc_savings", summary.excluded_accounts)
        self.assertIn("not_liquid_excluded", summary.labels)

    def test_a_debt_account_is_never_added_as_liquid_money(self):
        summary = consolidated_balance(self.balances(), rates={}, scope_policy=fx.policy())
        self.assertEqual(75000, summary.kopecks)
        self.assertNotIn("acc_loan", summary.included_accounts)

    def test_an_unknown_account_type_is_not_assumed_liquid(self):
        self.assertNotIn("crypto", LIQUID_ACCOUNT_TYPES)
        by_id = {b.account_id: b for b in self.balances()}
        self.assertFalse(by_id["acc_odd"].is_liquid)


# --------------------------------------------------------------------------- #
# 6. The real CLI over a private store and a fixture HTTP server
# --------------------------------------------------------------------------- #

class _DiffHandler(BaseHTTPRequestHandler):
    server_version = "randreport-fixture"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output quiet
        return

    def do_POST(self):  # noqa: N802 - the http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        self.server.recorded.append({"method": "POST", "path": self.path, "body": json.loads(raw or "{}")})
        body = json.dumps(self.server.provider()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.server.recorded.append({"method": "GET", "path": self.path, "body": None})
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FixtureDiffServer:
    """A loopback HTTP server answering ``POST /v8/diff/`` with synthetic rows."""

    def __init__(self, provider):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _DiffHandler)
        self.httpd.daemon_threads = True
        self.httpd.recorded = []
        self.httpd.provider = provider
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def recorded(self):
        return self.httpd.recorded

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class NightlyCliCase(unittest.TestCase):
    ROWS = [
        # one-account money moves, exactly as the real schema nests them
        {
            "id": "tx_rent",
            "date": "2025-07-03",
            "income": 0,
            "outcome": 1000.0,
            "incomeAccount": "acc_card",
            "outcomeAccount": "acc_card",
            "incomeInstrument": 2,
            "outcomeInstrument": 2,
            "tag": ["cat_rent"],
        },
        {
            "id": "tx_donation",
            "date": "2025-07-06",
            "income": 600.0,
            "outcome": 0,
            "incomeAccount": "acc_card",
            "outcomeAccount": "acc_card",
            "incomeInstrument": 2,
            "outcomeInstrument": 2,
            "tag": ["cat_donations"],
        },
        {
            "id": "tx_new_category",
            "date": "2025-07-24",
            "income": 300.0,
            "outcome": 0,
            "incomeAccount": "acc_card",
            "outcomeAccount": "acc_card",
            "incomeInstrument": 2,
            "outcomeInstrument": 2,
            "tag": ["cat_brand_new"],
        },
    ]

    # a June movement so the year/month reconciliation has two months of data
    JUNE_ROW = {
        "id": "tx_june_rent",
        "date": "2025-06-11",
        "income": 0,
        "outcome": 400.0,
        "incomeAccount": "acc_cash",
        "outcomeAccount": "acc_cash",
        "incomeInstrument": 2,
        "outcomeInstrument": 2,
        "tag": ["cat_rent"],
    }

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="randreport-nightly-cli-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = self.root / "randreport.sqlite"
        self.releases = self.root / "releases"
        self.lease = self.root / "nightly.lock"
        self.rows: list = []
        self.server = FixtureDiffServer(self._payload)
        self.addCleanup(self.server.close)
        conn = connect(str(self.db))
        apply_migrations(conn)
        conn.close()

    def _payload(self):
        return {
            "serverTimestamp": 1000,
            "instrument": [{"id": 2, "title": "RUB", "shortTitle": "RUB", "symbol": "₽"}],
            "transaction": list(self.rows),
            "account": [
                {
                    "id": "acc_card",
                    "type": "ccard",
                    "instrument": 2,
                    "balance": 5000.0,
                }
            ],
        }

    def record_contour(self):
        conn = connect(str(self.db))
        try:
            repo.record_category_mapping(conn, version="policy-1", payload=SYNTHETIC_PAYLOAD)
        finally:
            conn.close()

    def run_cli(self, argv: list[str]) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {
            "ZENMONEY_TOKEN": "synthetic-token",
            "ZENMONEY_BASE_URL": self.server.base_url,
            "RANREPORT_DB": str(self.db),
        }
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("TEAMUP_", "TELEGRAM_", "NEWS_", "ZENMONEY_", "RANREPORT_"))}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ops_cli.main(argv)
        text = out.getvalue().strip().splitlines()
        report = json.loads(text[-1]) if text else {}
        return code, report, err.getvalue()

    def nightly(self, *periods: str) -> tuple[int, dict, str]:
        argv = [
            "run",
            "--db",
            str(self.db),
            "--publish-dir",
            str(self.releases),
            "--lease",
            str(self.lease),
        ]
        if periods:
            argv += ["--period", ",".join(periods)]
        return self.run_cli(argv)

    def money(self, release_id: str) -> dict:
        path = self.releases / release_id / "money.json"
        self.assertTrue(path.exists(), f"no published release {release_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_a_missing_mapping_file_is_refused_without_a_contour(self):
        self.rows = list(self.ROWS)
        code, _, err = self.run_cli(
            [
                "run",
                "--db",
                str(self.db),
                "--publish-dir",
                str(self.releases),
                "--lease",
                str(self.lease),
                "--period",
                "2025-07",
                "--policy",
                str(self.root / "absent.json"),
            ]
        )
        self.assertEqual(5, code)
        self.assertIn("cannot load the finance policy", err)
        self.assertFalse(self.releases.exists() and any(self.releases.iterdir()))

    def test_a_private_mapping_file_drives_the_projection(self):
        self.rows = list(self.ROWS)
        path = self.root / "finance_policy.json"
        path.write_text(json.dumps(dict(SYNTHETIC_PAYLOAD, version="file-policy-1")), encoding="utf-8")
        code, report, err = self.run_cli(
            [
                "run",
                "--db",
                str(self.db),
                "--publish-dir",
                str(self.releases),
                "--lease",
                str(self.lease),
                "--period",
                "2025-07",
                "--policy",
                str(path),
            ]
        )
        self.assertEqual(0, code, err)
        money = self.money(report["release_id"])
        self.assertEqual("file-policy-1", report["policy_version"])
        self.assertEqual(100000, money["metrics"]["expense_kopecks"])
        self.assertEqual(60000, money["metrics"]["compensation_kopecks"])
        self.assertTrue(money["metrics"]["contour_confirmed"])

    def test_the_confirmed_map_drives_the_published_money(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        code, report, err = self.nightly("2025-07")
        self.assertEqual(0, code, err)
        money = self.money(report["release_id"])
        metrics = money["metrics"]
        self.assertEqual(100000, metrics["expense_kopecks"])
        self.assertEqual(60000, metrics["compensation_kopecks"])
        self.assertTrue(metrics["contour_confirmed"])
        buckets = bucket_totals(money)
        self.assertEqual(100000, buckets["operating_expense"])
        self.assertEqual(60000, buckets["external_compensation"])
        self.assertEqual(30000, buckets["unclassified"])

    def test_without_a_confirmed_map_the_nightly_publishes_unknown_not_zero(self):
        self.rows = list(self.ROWS)
        code, report, err = self.nightly("2025-07")
        self.assertEqual(0, code, err)
        money = self.money(report["release_id"])
        self.assertIsNone(money["metrics"]["expense_kopecks"])
        self.assertIsNone(money["metrics"]["compensation_kopecks"])
        self.assertEqual("unknown", money["completeness"])
        self.assertFalse(money["metrics"]["contour_confirmed"])
        self.assertIn("contour_unconfirmed", money["metrics"]["status_labels"])

    def test_an_unmapped_category_never_reaches_the_donation_bucket(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        _, report, _ = self.nightly("2025-07")
        money = self.money(report["release_id"])
        support = [row for row in money["categories"] if row["bucket"] in {"external_compensation", "commune_subsidy"}]
        self.assertEqual([60000], [row["amount_kopecks"] for row in support])
        self.assertNotIn("cat_brand_new", json.dumps(money))

    def test_a_desk_mapping_edit_changes_the_projection_on_the_next_run(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        _, first, _ = self.nightly("2025-07")
        before = self.money(first["release_id"])
        self.assertEqual(30000, bucket_totals(before)["unclassified"])

        from randreport.admin import desk

        conn = connect(str(self.db))
        try:
            desk.set_category_mapping(
                conn, category_id="cat_brand_new", economic_class="operating_expense", label="Новая статья", actor="desk"
            )
        finally:
            conn.close()

        _, second, _ = self.nightly("2025-07")
        after = self.money(second["release_id"])
        buckets = bucket_totals(after)
        self.assertEqual(130000, buckets["operating_expense"])
        self.assertNotIn("unclassified", buckets)
        labels = {row["amount_kopecks"]: row["label"] for row in after["categories"]}
        self.assertEqual("Новая статья", labels[30000])

    def test_a_repeat_run_after_an_edit_and_a_delete_follows_the_source(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        self.nightly("2025-07")
        self.rows = [
            {**self.ROWS[0], "outcome": 1200.0},
            {**self.ROWS[1], "deleted": True},
            self.ROWS[2],
        ]
        _, report, err = self.nightly("2025-07")
        money = self.money(report["release_id"])
        self.assertEqual(120000, money["metrics"]["expense_kopecks"])
        self.assertEqual(0, money["metrics"]["compensation_kopecks"])
        self.assertEqual(30000, money["metrics"]["unclassified_in_kopecks"])

    def test_month_and_year_agree(self):
        self.rows = [self.JUNE_ROW, *self.ROWS]
        self.record_contour()
        code, report, err = self.nightly("2025-06", "2025-07", "2025")
        self.assertEqual(0, code, err)
        releases = report["releases"]
        june = self.money(releases["2025-06"])
        july = self.money(releases["2025-07"])
        year = self.money(releases["2025"])
        for field in ("expense_kopecks", "gross_expense_kopecks", "compensation_kopecks", "external_compensation_kopecks"):
            self.assertEqual(
                june["metrics"][field] + july["metrics"][field],
                year["metrics"][field],
                field,
            )
        self.assertEqual(140000, year["metrics"]["expense_kopecks"])
        self.assertEqual(60000, year["metrics"]["compensation_kopecks"])

    def test_the_default_run_covers_every_whole_period_and_the_current_one(self):
        self.rows = [self.JUNE_ROW, *self.ROWS]
        self.record_contour()
        code, report, err = self.nightly()
        self.assertEqual(0, code, err)
        releases = set(report["releases"])
        self.assertEqual({"2025", "2025-06", "2025-07", "2026"}, releases - {self.current_month()})
        current = report["releases"][self.current_month()]
        money = self.money(current)
        self.assertFalse(money["period"]["complete"])
        self.assertIn("period_incomplete", money["metrics"]["status_labels"])
        # the year of the unfinished current month is published too, and its
        # coverage is the ratio of the year's sums, not the mean of the months
        year = self.money(report["releases"][str(self.current_month()[:4])])
        self.assertFalse(year["period"]["complete"])
        self.assertIn("period_incomplete", year["metrics"]["status_labels"])

    @staticmethod
    def current_month() -> str:
        now = datetime.now(timezone.utc).astimezone(MSK)
        return f"{now.year:04d}-{now.month:02d}"

    def test_no_private_identifier_reaches_the_public_payload(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        _, report, _ = self.nightly("2025-07", "2025")
        patterns = (
            re.compile(r"(?<![A-Za-z0-9])(?:acc|occ|cat|sub|cal|pair|tx)_[A-Za-z0-9_]+"),
            re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        )
        for release_id in report["releases"].values():
            for section in ("money", "events", "community", "chronicle"):
                path = self.releases / release_id / f"{section}.json"
                text = path.read_text(encoding="utf-8")
                for pattern in patterns:
                    self.assertEqual([], pattern.findall(text), f"{release_id}/{section}")
                self.assertEqual((), find_leaks(json.loads(text)))
        self.assertNotIn("acc_card", json.dumps(report["releases"]))

    def test_the_api_receives_zero_write_requests(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        code, _, err = self.nightly("2025-07")
        self.assertEqual(0, code, err)
        self.assertTrue(self.server.recorded)
        for request in self.server.recorded:
            self.assertEqual("POST", request["method"])
            self.assertTrue(request["path"].endswith("/v8/diff/"), request["path"])
            self.assertEqual({"currentClientTimestamp", "serverTimestamp"}, set(request["body"]))
            for forbidden in ("transaction", "account", "tag", "deletion", "budget", "instrument", "user", "merchant"):
                self.assertNotIn(forbidden, request["body"])

    def test_a_dry_run_reads_the_sources_but_publishes_nothing(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        code, report, err = self.run_cli(
            [
                "run",
                "--db",
                str(self.db),
                "--publish-dir",
                str(self.releases),
                "--lease",
                str(self.lease),
                "--period",
                "2025-07",
                "--dry-run",
            ]
        )
        self.assertEqual(0, code, err)
        self.assertFalse(report["published"])
        self.assertEqual({}, report["releases"])
        self.assertFalse(self.releases.exists())
        # the facts of the read are kept: the next run publishes from them
        conn = connect(str(self.db))
        try:
            self.assertEqual(3, int(conn.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"]))
        finally:
            conn.close()

    def test_the_nightly_never_rewrites_the_desk_mapping(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        self.nightly("2025-07", "2025")
        conn = connect(str(self.db))
        try:
            versions = [str(row["version"]) for row in conn.execute("SELECT version FROM category_mapping_versions")]
            annotations = int(conn.execute("SELECT COUNT(*) AS n FROM finance_annotations").fetchone()["n"])
        finally:
            conn.close()
        self.assertEqual(["policy-1"], versions)
        self.assertEqual(0, annotations)

    def test_balances_are_captured_with_an_explicit_liquidity(self):
        self.rows = list(self.ROWS)
        self.record_contour()
        self.nightly("2025-07")
        conn = connect(str(self.db))
        try:
            rows = {str(row["account_id"]): row for row in conn.execute("SELECT * FROM account_balance_snapshots")}
        finally:
            conn.close()
        self.assertIn("acc_card", rows)
        self.assertEqual(1, int(rows["acc_card"]["is_liquid"]))
        self.assertEqual(500000, int(rows["acc_card"]["balance_kopecks"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
