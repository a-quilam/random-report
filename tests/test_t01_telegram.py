"""T01 — Telegram subscriber points: a gap is a gap, an error is never a zero.

docs/REQUIREMENTS.md section 8 and the T01 row of the acceptance matrix.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.connectors.telegram import (  # noqa: E402
    HistoryUnavailable,
    MTProtoHistorySource,
    TelegramClient,
    TelegramError,
)
from randreport.connectors.transport import TransportError, json_response  # noqa: E402
from randreport.domain.timeframes import MSK  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402
from tests.fixtures_ingest import RecordingTransport  # noqa: E402


class TestT01Snapshot(unittest.TestCase):
    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")

    def tearDown(self):
        self.tmp.cleanup()
        self.conn.close()

    def community(self):
        return json.loads(repo.current_release(self.conn)["payload"])["community"]

    def test_a_successful_snapshot_becomes_a_dated_point(self):
        client, transport = ig.telegram(posts=[ig.post("600", "пост")], members=4128)
        report = ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        self.assertEqual("ok", report.sources["telegram"].status)
        points = repo.channel_points(self.conn)
        self.assertEqual(1, len(points))
        self.assertEqual(4128, points[0][1])
        self.assertTrue(any("getChatMemberCount" in request.url for request in transport.requests))

    def test_a_transport_error_is_a_gap_not_a_zero(self):
        client, _ = ig.telegram(posts=[], members=TransportError("bot api down"))
        report = ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        self.assertEqual("partial", report.sources["telegram"].status)
        self.assertEqual((), repo.channel_points(self.conn))
        self.assertIsNone(self.community()["metrics"]["latest_subscribers"])
        self.assertIn("telegram_snapshot_missing", {issue["code"] for issue in repo.issues(self.conn)})

    def test_an_api_error_response_is_not_a_zero(self):
        def handler(request):
            return json_response({"ok": False, "description": "Bad Request: chat not found"})

        client = TelegramClient(
            RecordingTransport(handler),
            bot_token="bot-token",
            history=ig.StaticPostSource([]),
            allowed_channel_ids=("@random_culture",),
        )
        report = ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        self.assertEqual("partial", report.sources["telegram"].status)
        self.assertEqual((), repo.channel_points(self.conn))
        payload = json.loads(repo.current_release(self.conn)["payload"])
        self.assertEqual(0, payload["community"]["metrics"]["points"])

    def test_no_daily_interpolation_only_real_points(self):
        first, _ = ig.telegram(posts=[], members=4000)
        ig.run_once(self.conn, [ig.telegram_adapter(first)], now=datetime(2025, 7, 5, 3, 0, tzinfo=MSK), lease=self.lease)
        second, _ = ig.telegram(posts=[], members=4100)
        ig.run_once(self.conn, [ig.telegram_adapter(second)], now=datetime(2025, 7, 20, 3, 0, tzinfo=MSK), lease=self.lease)

        metrics = self.community()["metrics"]
        self.assertEqual(2, metrics["points"])
        self.assertEqual(100, metrics["change"])
        self.assertEqual("2025-07-05", metrics["change_from"])
        self.assertEqual("2025-07-20", metrics["change_to"])
        self.assertFalse(metrics["interpolated"])

    def test_a_single_point_has_no_fabricated_change(self):
        client, _ = ig.telegram(posts=[], members=4128)
        ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        metrics = self.community()["metrics"]
        self.assertEqual(1, metrics["points"])
        self.assertIsNone(metrics["change"])
        self.assertIsNone(metrics["change_from"])

    def test_the_client_refuses_channels_outside_the_allowlist(self):
        client, _ = ig.telegram(posts=[], members=4128)
        with self.assertRaises(TelegramError):
            client.pull_posts(channel_id="@someone_else")

    def test_the_read_only_history_source_is_reported_as_blocked_not_faked(self):
        source = MTProtoHistorySource(allowed_channel_ids=("@random_culture",))
        with self.assertRaises(HistoryUnavailable):
            source.fetch(since_id=None, limit=10)

    def test_history_without_a_source_is_unavailable_not_a_fabricated_post(self):
        client = TelegramClient(
            RecordingTransport(lambda request: json_response({"ok": True, "result": 1})),
            bot_token="bot-token",
            history=None,
            allowed_channel_ids=("@random_culture",),
        )
        with self.assertRaises(HistoryUnavailable):
            client.pull_posts(channel_id="@random_culture")

    def test_the_bot_token_never_appears_in_the_client_repr(self):
        client, _ = ig.telegram(posts=[], members=1, token="SECRET-BOT-TOKEN-9")
        self.assertNotIn("SECRET-BOT-TOKEN-9", repr(client))

    def test_posts_are_archived_and_edits_bump_the_revision(self):
        client, _ = ig.telegram(posts=[ig.post("600", "первая версия")], members=4128)
        ig.run_once(self.conn, [ig.telegram_adapter(client)], lease=self.lease)
        archived = repo.archived_posts(self.conn)
        self.assertEqual(1, len(archived))
        self.assertEqual("первая версия", archived[0].text)

        edited = ig.post("600", "исправленная версия", edited=datetime(2025, 7, 13, 12, 0, tzinfo=MSK))
        counts = repo.upsert_posts(self.conn, [edited])
        self.assertEqual(1, counts.changed)
        self.assertEqual("исправленная версия", repo.archived_posts(self.conn)[0].text)


if __name__ == "__main__":
    unittest.main()
