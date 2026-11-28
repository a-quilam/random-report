"""S03 — the 03:00 Europe/Moscow cycle: lease, idempotency, catch-up, isolation.

docs/REQUIREMENTS.md section 9 and the S03 row of the acceptance matrix.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.connectors.transport import TransportError  # noqa: E402
from randreport.domain.timeframes import MSK  # noqa: E402
from randreport.ops.run import RunInProgress, RunLease, NightlyRunner  # noqa: E402
from randreport.ops.schedule import (  # noqa: E402
    SYSTEMD_CALENDAR,
    is_due,
    logical_date_for,
    missed_logical_dates,
    next_run_at,
    systemd_timer_text,
)
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402

ROWS = [ig.zen_row("t1", "2025-07-05", 100_000, account="acc_card", tag=["cat_rent"])]


class TestS03Schedule(unittest.TestCase):
    def test_next_run_is_the_next_0300_moscow(self):
        self.assertEqual(datetime(2025, 7, 31, 3, 0, tzinfo=MSK), next_run_at(datetime(2025, 7, 31, 1, 0, tzinfo=MSK)))
        self.assertEqual(datetime(2025, 8, 1, 3, 0, tzinfo=MSK), next_run_at(datetime(2025, 7, 31, 3, 0, tzinfo=MSK)))
        self.assertEqual(datetime(2025, 8, 1, 3, 0, tzinfo=MSK), next_run_at(datetime(2025, 7, 31, 23, 30, tzinfo=MSK)))

    def test_the_schedule_does_not_depend_on_the_host_timezone(self):
        as_moscow = datetime(2025, 7, 31, 1, 30, tzinfo=MSK)
        as_utc = as_moscow.astimezone(timezone.utc)
        as_nairobi = as_moscow.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(next_run_at(as_moscow), next_run_at(as_utc))
        self.assertEqual(next_run_at(as_moscow), next_run_at(as_nairobi))

    def test_naive_datetimes_are_refused(self):
        with self.assertRaises(ValueError):
            next_run_at(datetime(2025, 7, 31, 1, 0))

    def test_logical_date_is_the_moscow_day(self):
        self.assertEqual("2025-08-01", logical_date_for(datetime(2025, 7, 31, 22, 30, tzinfo=timezone.utc)))

    def test_due_only_after_0300_and_only_once_per_moscow_day(self):
        self.assertFalse(is_due(datetime(2025, 7, 31, 2, 59, tzinfo=MSK), None))
        self.assertTrue(is_due(datetime(2025, 7, 31, 3, 0, tzinfo=MSK), None))
        self.assertFalse(is_due(datetime(2025, 7, 31, 3, 0, tzinfo=MSK), "2025-07-31"))

    def test_missed_days_are_recovered_without_becoming_an_hourly_cadence(self):
        self.assertEqual(
            ("2025-07-29", "2025-07-30"),
            missed_logical_dates("2025-07-28", datetime(2025, 7, 31, 4, 0, tzinfo=MSK)),
        )
        self.assertEqual((), missed_logical_dates("2025-07-30", datetime(2025, 7, 31, 4, 0, tzinfo=MSK)))
        self.assertEqual(
            ("2025-07-31",),
            missed_logical_dates(None, datetime(2025, 7, 31, 4, 0, tzinfo=MSK)),
        )

    def test_systemd_calendar_is_pinned_to_europe_moscow(self):
        self.assertEqual("*-*-* 03:00:00 Europe/Moscow", SYSTEMD_CALENDAR)
        timer = systemd_timer_text()
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow", timer)
        self.assertIn("Persistent=true", timer)


class TestS03Lease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lease_path = Path(self.tmp.name) / "nightly.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_second_concurrent_run_is_refused(self):
        first = RunLease(self.lease_path)
        first.acquire()
        try:
            with self.assertRaises(RunInProgress):
                RunLease(self.lease_path).acquire()
        finally:
            first.release()

    def test_the_lease_is_released_after_the_cycle(self):
        conn = ig.fresh_conn()
        client, _ = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        ig.run_once(conn, [ig.zenmoney_adapter(client)], lease=str(self.lease_path))
        with RunLease(self.lease_path):  # free again
            pass
        conn.close()

    def test_a_concurrent_cycle_raises_instead_of_double_publishing(self):
        conn = ig.fresh_conn()
        client, _ = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        with RunLease(self.lease_path):
            runner = NightlyRunner(
                conn,
                adapters=[ig.zenmoney_adapter(client)],
                builder=ig.builder(),
                lease_path=self.lease_path,
                retries=0,
            )
            with self.assertRaises(RunInProgress):
                runner.run(now=ig.NOW)
        conn.close()


class TestS03Idempotency(unittest.TestCase):
    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")

    def tearDown(self):
        self.tmp.cleanup()
        self.conn.close()

    def test_a_second_attempt_on_the_same_day_reuses_the_logical_run(self):
        client, _ = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        first = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        second = ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        self.assertEqual(first.logical_date, second.logical_date)
        self.assertEqual(1, len(repo.source_runs(self.conn, "zenmoney")))
        self.assertEqual(1, len(repo.source_runs(self.conn, "zenmoney")))

    def test_the_subscriber_snapshot_is_not_duplicated_on_the_same_day(self):
        client, _ = ig.telegram(posts=[ig.post("600", "пост")], members=4128)
        ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        rows = self.conn.execute("SELECT COUNT(*) AS n FROM channel_snapshots").fetchone()["n"]
        self.assertEqual(1, rows)
        self.assertEqual(1, len(repo.channel_points(self.conn)))

    def test_a_changed_value_on_the_same_day_bumps_the_revision(self):
        repo.record_channel_snapshot(self.conn, on_date="2025-07-31", subscribers=4128, source="telegram", received_at=ig.NOW)
        self.assertEqual(
            "revised",
            repo.record_channel_snapshot(self.conn, on_date="2025-07-31", subscribers=4130, source="telegram", received_at=ig.NOW),
        )
        row = self.conn.execute("SELECT subscribers, revision FROM channel_snapshots WHERE on_date = '2025-07-31'").fetchone()
        self.assertEqual(4130, row["subscribers"])
        self.assertEqual(2, row["revision"])


class TestS03Isolation(unittest.TestCase):
    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")

    def tearDown(self):
        self.tmp.cleanup()
        self.conn.close()

    def test_one_failed_source_does_not_zero_another(self):
        zen, zen_transport = ig.zenmoney(rows=ROWS, server_timestamp=1000)
        zen_transport.error = TransportError("zenmoney down")
        teamup, _ = ig.teamup(
            pages=[{"events": [ig.teamup_event("occ1", "2025-07-10T18:00:00+03:00", "2025-07-10T20:00:00+03:00")], "pages": {"next": None}}]
        )
        report = ig.run_once(self.conn, [ig.zenmoney_adapter(zen), ig.teamup_adapter(teamup)], lease=self.lease)

        self.assertEqual("failed", report.sources["zenmoney"].status)
        self.assertEqual("ok", report.sources["teamup"].status)
        self.assertTrue(report.published)
        self.assertIsNotNone(report.statuses["teamup"].last_success_at)
        self.assertIsNone(report.statuses["zenmoney"].last_success_at)
        self.assertEqual(1, len(repo.load_occurrences(self.conn)))

        import json

        payload = json.loads(repo.current_release(self.conn)["payload"])
        # finance was never read: unknown, not a confirmed zero
        self.assertIsNone(payload["money"]["metrics"]["expense_kopecks"])
        self.assertIn("no_data", payload["money"]["metrics"]["status_labels"])
        # the calendar section still carries the events that were read
        self.assertEqual(1, payload["events"]["metrics"]["total_events"])


if __name__ == "__main__":
    unittest.main()
