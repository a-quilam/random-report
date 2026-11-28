"""S01 — Zenmoney read-only synchronisation: full diff, incremental diff, idempotency.

docs/REQUIREMENTS.md section 6.1 and the S01 row of the acceptance matrix.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.connectors.zenmoney import (  # noqa: E402
    FORBIDDEN_REQUEST_KEYS,
    READ_REQUEST_KEYS,
    ReadOnlyViolation,
    ZenmoneyClient,
)
from randreport.connectors.transport import TransportError  # noqa: E402
from randreport.domain.flows import EconomicClass  # noqa: E402
from randreport.domain.finance import ScopePolicy, build_ledger, classify_movement, compute_totals  # noqa: E402
from randreport.domain.timeframes import month_period  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402


class TestS01ReadOnlyRequest(unittest.TestCase):
    def test_only_synchronisation_markers_may_be_sent(self):
        ZenmoneyClient.assert_read_request({"currentClientTimestamp": 1, "serverTimestamp": 0})

    def test_change_arrays_are_refused(self):
        for key in sorted(FORBIDDEN_REQUEST_KEYS):
            with self.subTest(key=key):
                with self.assertRaises(ReadOnlyViolation):
                    ZenmoneyClient.assert_read_request({"currentClientTimestamp": 1, "serverTimestamp": 0, key: []})

    def test_keys_outside_the_read_allowlist_are_refused(self):
        with self.assertRaises(ReadOnlyViolation):
            ZenmoneyClient.assert_read_request({"currentClientTimestamp": 1, "serverTimestamp": 0, "note": "x"})

    def test_the_client_repr_never_leaks_the_token(self):
        client, _ = ig.zenmoney(rows=[], token="SECRET-ZENMONEY-TOKEN-42")
        self.assertNotIn("SECRET-ZENMONEY-TOKEN-42", repr(client))


class TestS01DiffFlow(unittest.TestCase):
    def setUp(self):
        self.conn = ig.fresh_conn()
        self.addCleanup(self.conn.close)
        self.rows = [
            ig.zen_row("t1", "2025-07-05", 100_000, account="acc_card", tag=["cat_rent"]),
            ig.zen_row("t2", "2025-07-06", 60_000, account="acc_card", direction="in", tag=["cat_donations"]),
        ]

    def test_full_diff_sends_zero_marker_and_returns_the_server_timestamp(self):
        client, transport = ig.zenmoney(rows=self.rows, server_timestamp=1000)
        report = ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        self.assertEqual("ok", report.sources["zenmoney"].status)
        self.assertEqual(0, transport.requests[0].json_body["serverTimestamp"])
        self.assertEqual(("1000", 1), repo.get_cursor(self.conn, "zenmoney"))
        self.assertEqual(2, len(repo.load_movements(self.conn)))

    def test_incremental_diff_resumes_from_the_committed_cursor(self):
        client, transport = ig.zenmoney(rows=self.rows, server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        transport.handler = lambda request: ig.json_response(ig.zen_payload([], server_timestamp=2000))
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)], now=ig.NOW.replace(day=1, month=8))
        self.assertEqual(1000, transport.requests[-1].json_body["serverTimestamp"])
        self.assertEqual(("2000", 2), repo.get_cursor(self.conn, "zenmoney"))

    def test_repeated_payload_does_not_duplicate_and_counts_as_unchanged(self):
        client, transport = ig.zenmoney(rows=self.rows, server_timestamp=1000)
        first = ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        self.assertEqual(2, first.sources["zenmoney"].counts.new)
        rows_after_first = self.conn.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"]
        second = ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        self.assertEqual(2, second.sources["zenmoney"].counts.unchanged)
        self.assertEqual(0, second.sources["zenmoney"].counts.new)
        self.assertEqual(rows_after_first, self.conn.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"])

    def test_edit_updates_the_row_in_place(self):
        client, transport = ig.zenmoney(rows=self.rows, server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        transport.handler = lambda request: ig.json_response(
            ig.zen_payload([ig.zen_row("t1", "2025-07-05", 120_000, account="acc_card", tag=["cat_rent"])], server_timestamp=1001)
        )
        report = ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        self.assertEqual(1, report.sources["zenmoney"].counts.changed)
        amounts = {m.id: m.amount_kopecks for m in repo.load_movements(self.conn)}
        self.assertEqual(120_000, amounts["t1"])
        self.assertEqual(2, self.conn.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"])

    def test_deletion_keeps_the_row_and_the_revision_but_leaves_the_totals(self):
        client, transport = ig.zenmoney(rows=self.rows, server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        transport.handler = lambda request: ig.json_response(ig.zen_payload([ig.zen_row("t1", "2025-07-05", 0, deleted=True)], server_timestamp=1002))
        report = ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        self.assertEqual(1, report.sources["zenmoney"].counts.deleted)
        row = self.conn.execute("SELECT deleted FROM finance_transactions WHERE transaction_id = 't1'").fetchone()
        self.assertEqual(1, row["deleted"])
        ledger = build_ledger(repo.load_movements(self.conn), fx.policy(), month_period("2025-07"))
        totals = compute_totals(ledger)
        self.assertEqual(60_000, totals.compensation_kopecks)
        self.assertEqual(0, totals.expense_kopecks)
        self.assertIn("t1", ledger.deleted_ids)

    def test_cursor_is_not_advanced_when_the_source_fails(self):
        client, transport = ig.zenmoney(rows=self.rows, server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        transport.error = TransportError("connection reset")
        report = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], now=ig.NOW.replace(month=8, day=1))
        self.assertEqual("failed", report.sources["zenmoney"].status)
        self.assertEqual(("1000", 1), repo.get_cursor(self.conn, "zenmoney"))
        self.assertEqual(2, len(repo.load_movements(self.conn)))

    def test_zero_amount_row_is_not_turned_into_a_movement(self):
        client, transport = ig.zenmoney(
            rows=[{"id": "t0", "date": "2025-07-05T12:00:00+03:00", "income": 0, "outcome": 0, "tag": []}],
            server_timestamp=1000,
        )
        report = ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        self.assertEqual(0, len(repo.load_movements(self.conn)))
        self.assertEqual(1, report.sources["zenmoney"].payload["skipped_zero"])
        self.assertEqual(0, report.sources["zenmoney"].payload["rows_read"])
        self.assertEqual(0, report.sources["zenmoney"].counts.new)

    def test_two_account_row_becomes_one_internal_transfer(self):
        client, _ = ig.zenmoney(rows=[ig.zen_transfer("tr1", "2025-07-07", 5_000, source="acc_cash", target="acc_card")], server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        ledger = build_ledger(repo.load_movements(self.conn), fx.policy(), month_period("2025-07"))
        totals = compute_totals(ledger)
        self.assertEqual(5_000, totals.internal_transfer_kopecks)
        self.assertEqual(0, totals.expense_kopecks)
        self.assertEqual(0, totals.compensation_kopecks)
        self.assertEqual(1, len(ledger.internal_transfer_pairs))
        internal = [c for c in ledger.classifications if c.economic_class is EconomicClass.INTERNAL_TRANSFER]
        self.assertEqual(1, len(internal))

    def test_unknown_instrument_is_flagged_not_silently_rubles(self):
        client, _ = ig.zenmoney(
            rows=[ig.zen_row("t9", "2025-07-05", 1000, account="acc_card", tag=["cat_rent"], outcome_instrument=99)],
            server_timestamp=1000,
        )
        report = ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        self.assertEqual(["99"], report.sources["zenmoney"].payload["currency_unknown"])
        self.assertEqual("partial", report.sources["zenmoney"].coverage)

    def test_read_only_client_only_ever_posts_to_the_diff_endpoint(self):
        client, transport = ig.zenmoney(rows=[], server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)])
        request = transport.requests[0]
        self.assertEqual("POST", request.method)
        self.assertTrue(request.url.endswith("/v8/diff/"))
        self.assertEqual(READ_REQUEST_KEYS, frozenset(request.json_body))


if __name__ == "__main__":
    unittest.main()
