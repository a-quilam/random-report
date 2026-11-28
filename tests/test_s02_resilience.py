"""S02 — a failed or partial cycle keeps the last validated snapshot.

docs/REQUIREMENTS.md section 9 and the S02 row of the acceptance matrix.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.connectors.telegram import TelegramClient  # noqa: E402
from randreport.connectors.transport import TransportError  # noqa: E402
from randreport.ops.pipeline import ReleaseBuilder  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402

ROWS = [
    ig.zen_row("t1", "2025-07-05", 100_000, account="acc_card", tag=["cat_rent"]),
    ig.zen_row("t2", "2025-07-06", 60_000, account="acc_card", direction="in", tag=["cat_donations"]),
]


class ExplodingBuilder(ReleaseBuilder):
    """A builder that fails, to prove an unsafe revision is never published."""

    def build(self, *args, **kwargs):  # noqa: D102
        raise RuntimeError("synthetic builder failure")


class TestS02LastGood(unittest.TestCase):
    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = f"{self.tmp.name}/nightly.lock"

    def tearDown(self):
        self.tmp.cleanup()
        self.conn.close()

    def test_a_failed_run_keeps_last_good_and_does_not_advance_last_success(self):
        client, transport = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        first = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        self.assertTrue(first.published)
        good_payload = repo.last_good(self.conn, "zenmoney")["payload"]
        success_at = repo.source_runs(self.conn, "zenmoney")[-1]["last_success_at"]
        release_id = first.release_id

        transport.error = TransportError("source down")
        second = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], now=ig.NOW + timedelta(days=1), lease=self.lease)

        self.assertEqual("failed", second.sources["zenmoney"].status)
        self.assertEqual(good_payload, repo.last_good(self.conn, "zenmoney")["payload"])
        successes = [row["last_success_at"] for row in repo.source_runs(self.conn, "zenmoney") if row["last_success_at"]]
        self.assertEqual([success_at], successes)
        self.assertEqual(2, len(repo.load_movements(self.conn)))
        # the last validated numbers keep serving; a failed source never writes a zero
        first_money = json.loads(repo.release(self.conn, release_id)["payload"])["money"]
        served_money = json.loads(repo.current_release(self.conn)["payload"])["money"]
        self.assertEqual(first_money["metrics"]["expense_kopecks"], served_money["metrics"]["expense_kopecks"])
        self.assertEqual(first_money["metrics"]["compensation_kopecks"], served_money["metrics"]["compensation_kopecks"])
        self.assertEqual(
            [c["amount_kopecks"] for c in first_money["categories"]],
            [c["amount_kopecks"] for c in served_money["categories"]],
        )
        self.assertIn("source_failed", {issue["code"] for issue in repo.issues(self.conn)})

    def test_recovery_advances_last_success_and_republishes(self):
        client, transport = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        transport.error = TransportError("source down")
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)], now=ig.NOW + timedelta(days=1), lease=self.lease)

        # the transient failure is recorded against the logical date of that run
        codes = {(issue["code"], issue["status"]) for issue in repo.issues(self.conn)}
        self.assertIn(("source_failed", "open"), codes)

        transport.error = None
        transport.handler = lambda request: ig.json_response(
            ig.zen_payload([ig.zen_row("t3", "2025-08-02", 5_000, account="acc_card", tag=["cat_utilities"])], server_timestamp=2000)
        )
        third = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], now=ig.NOW + timedelta(days=2), lease=self.lease)
        self.assertEqual("ok", third.sources["zenmoney"].status)
        self.assertTrue(third.published)
        self.assertEqual(3, len(repo.load_movements(self.conn)))
        self.assertEqual(("2000", 2), repo.get_cursor(self.conn, "zenmoney"))

        # a source that reads successfully is no longer failing: the per-date
        # lifecycle issue is closed by the cycle itself (nothing else can match
        # its date-bearing id), with the row and an audit trail kept
        issues = {issue["issue_id"]: issue for issue in repo.issues(self.conn)}
        failed = [issue for issue in issues.values() if issue["code"] == "source_failed"]
        self.assertTrue(failed)
        for issue in failed:
            self.assertEqual("resolved", issue["status"])
        self.assertEqual(1, third.sources["zenmoney"].payload["issues_resolved"])
        resolved_audit = self.conn.execute("SELECT entity_key FROM audit_log WHERE action = 'issue.resolved'").fetchall()
        self.assertTrue(resolved_audit)
        # the healthy source raises no issue of its own
        self.assertNotIn(("source_failed", "open"), {(issue["code"], issue["status"]) for issue in repo.issues(self.conn)})

    def test_a_history_source_that_is_not_configured_is_a_gap_without_a_nightly_issue(self):
        """The documented normal state of the news intake must not nag every night."""

        _, transport = ig.telegram(posts=[])
        client = TelegramClient(
            transport,
            bot_token="bot-token",
            history=None,  # no approved read-only session: the packaged normal state
            allowed_channel_ids=("@random_culture",),
        )
        report = ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        self.assertEqual("ok", report.sources["telegram"].status)  # the snapshot is its own job
        payload = report.sources["telegram"].payload
        self.assertEqual("not_configured", payload["history"])
        self.assertIsNone(payload["history_error"])
        self.assertEqual(0, payload["posts_read"])
        self.assertEqual((), tuple(issue for issue in repo.issues(self.conn) if issue["code"] == "telegram_history_unavailable"))

    def test_a_partial_source_never_writes_a_zero_and_keeps_its_last_good(self):
        client, transport = ig.telegram(posts=[ig.post("600", "обычный анонс")], members=TransportError("bot api down"))
        report = ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        self.assertEqual("partial", report.sources["telegram"].status)
        self.assertEqual((), repo.channel_points(self.conn))
        self.assertIsNone(repo.last_good(self.conn, "telegram"))
        self.assertIn("telegram_snapshot_missing", {issue["code"] for issue in repo.issues(self.conn)})
        payload = json.loads(repo.current_release(self.conn)["payload"])
        self.assertIsNone(payload["community"]["metrics"]["latest_subscribers"])
        self.assertEqual(0, payload["community"]["metrics"]["points"])

    def test_a_failed_release_build_is_not_published_and_the_previous_release_serves(self):
        client, _ = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        first = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        self.assertTrue(first.published)

        report = ig.run_once(
            self.conn,
            [ig.zenmoney_adapter(client)],
            now=ig.NOW + timedelta(days=1),
            release_builder=ExplodingBuilder(policy=ig.builder().policy, event_mapping=ig.builder().event_mapping, period=ig.builder().period),
            lease=self.lease,
        )
        self.assertFalse(report.published)
        self.assertIsNone(report.release_id)
        self.assertEqual(first.release_id, repo.current_release(self.conn)["release_id"])
        self.assertIn("release_build_failed", {issue["code"] for issue in repo.issues(self.conn)})
        self.assertIn("release_build_failed", report.issues)

    def test_an_unchanged_day_yields_the_same_release_id(self):
        client, _ = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        first = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        second = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        self.assertEqual(first.release_id, second.release_id)


if __name__ == "__main__":
    unittest.main()
