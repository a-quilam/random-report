"""The domain-less Telegram intake: a ``getUpdates`` poll instead of a webhook.

The card (``t_1b5b08c8``) fixes the rules this pins, all over
``docs/REQUIREMENTS.md`` section 8:

* the poll checks ``getWebhookInfo`` and **refuses while any webhook is
  registered** — it never calls ``setWebhook``/``deleteWebhook`` and never clears
  someone else's queue;
* only an allowed ``channel_post`` / ``edited_channel_post`` is stored; a foreign
  chat is refused, a non-post update is ignored, a malformed one is counted;
* the offset is durable and is advanced **only after** the inbox rows committed;
* nothing here makes a network call: a ``FakeTransport`` answers, and one test
  drives the real CLI with the transport patched in.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.connectors.transport import FakeTransport, json_response  # noqa: E402
from randreport.inbox import polling  # noqa: E402
from randreport.inbox.polling import (  # noqa: E402
    OFFSET_KEY,
    PollingError,
    WebhookActive,
    fetch_updates,
    ingest_updates,
    poll_into_inbox,
    read_webhook_info,
    stored_offset,
)
from randreport.inbox.store import inbox_counts  # noqa: E402
from randreport.store import apply_migrations, connect  # noqa: E402

CHANNEL = "@random_culture"
FOREIGN = "@someone_else"
CHANNEL_ID = -1001234567890
POST_TS = int(datetime(2025, 7, 19, 7, 0, tzinfo=timezone.utc).timestamp())
BASE = "https://api.telegram.org"
TOKEN = "123456:TEST-TOKEN-NOT-REAL"


def _db():
    conn = connect(":memory:")
    apply_migrations(conn)
    return conn


def _post_update(update_id: int, *, message_id: int = 100, username: str = "random_culture", text: str = "привет") -> dict:
    return {
        "update_id": update_id,
        "channel_post": {
            "message_id": message_id,
            "date": POST_TS,
            "text": text,
            "chat": {"id": CHANNEL_ID, "username": username, "type": "channel"},
        },
    }


def _webhook_info(url: str = "", pending: int = 0):
    return json_response({"ok": True, "result": {"url": url, "pending_update_count": pending}})


def _updates(items):
    return json_response({"ok": True, "result": items})


def _router(webhook=None, updates=None):
    def handler(request):
        if request.url.endswith("/getWebhookInfo"):
            return webhook if webhook is not None else _webhook_info()
        if request.url.endswith("/getUpdates"):
            return updates if updates is not None else _updates([])
        raise AssertionError(f"unexpected call {request.url}")

    return handler


class WebhookInfoTests(unittest.TestCase):
    def test_a_registered_webhook_is_reported_as_set(self):
        transport = FakeTransport(_router(webhook=_webhook_info("https://example.test/hook", pending=5)))
        state = read_webhook_info(transport, bot_token=TOKEN, base_url=BASE)
        self.assertTrue(state.url_set)
        self.assertEqual(5, state.pending_update_count)

    def test_a_missing_token_is_refused_not_guessed(self):
        with self.assertRaises(PollingError):
            read_webhook_info(FakeTransport(_router()), bot_token=None, base_url=BASE)


class FetchUpdatesTests(unittest.TestCase):
    def test_posts_and_edits_only_and_no_offset_without_state(self):
        transport = FakeTransport(_router(updates=_updates([])))
        fetch_updates(transport, bot_token=TOKEN, base_url=BASE, offset=None)
        request = transport.last()
        self.assertNotIn("offset", request.params)
        self.assertEqual(["channel_post", "edited_channel_post"], json.loads(request.params["allowed_updates"]))
        self.assertEqual("0", request.params["timeout"])

    def test_a_durable_offset_is_passed(self):
        transport = FakeTransport(_router(updates=_updates([])))
        fetch_updates(transport, bot_token=TOKEN, base_url=BASE, offset=42)
        self.assertEqual("42", transport.last().params["offset"])


class IngestTests(unittest.TestCase):
    def test_allowed_post_is_stored_and_foreign_is_refused(self):
        conn = _db()
        try:
            counts, highest = ingest_updates(
                conn,
                [_post_update(1), _post_update(2, message_id=101, username="someone_else")],
                allowed_channels=[CHANNEL],
            )
            self.assertEqual(2, counts["received"])
            self.assertEqual(1, counts["accepted"])
            self.assertEqual(1, counts["foreign"])
            self.assertEqual(2, highest)
            self.assertEqual(1, inbox_counts(conn)["messages"])
        finally:
            conn.close()

    def test_a_non_post_update_is_ignored_and_a_malformed_one_is_counted(self):
        conn = _db()
        try:
            counts, _ = ingest_updates(
                conn,
                [{"update_id": 3, "message": {"message_id": 1}}, {"update_id": 4, "channel_post": {"message_id": -1}}],
                allowed_channels=[CHANNEL],
            )
            self.assertEqual(1, counts["ignored"])
            self.assertEqual(1, counts["rejected"])
            self.assertEqual(0, inbox_counts(conn)["rows_total"])
        finally:
            conn.close()

    def test_an_edit_becomes_a_new_revision_of_the_same_message(self):
        conn = _db()
        try:
            ingest_updates(conn, [_post_update(1, text="первая")], allowed_channels=[CHANNEL])
            counts, _ = ingest_updates(
                conn,
                [
                    {
                        "update_id": 2,
                        "edited_channel_post": {
                            "message_id": 100,
                            "date": POST_TS,
                            "edit_date": POST_TS + 60,
                            "text": "вторая",
                            "chat": {"id": CHANNEL_ID, "username": "random_culture", "type": "channel"},
                        },
                    }
                ],
                allowed_channels=[CHANNEL],
            )
            self.assertEqual(1, counts["revised"])
        finally:
            conn.close()


class PollIntoInboxTests(unittest.TestCase):
    def test_a_registered_webhook_stops_the_poll_without_touching_it(self):
        conn = _db()
        try:
            transport = FakeTransport(_router(webhook=_webhook_info("https://example.test/hook")))
            with self.assertRaises(WebhookActive):
                poll_into_inbox(conn, transport, bot_token=TOKEN, allowed_channels=[CHANNEL], base_url=BASE)
            # only getWebhookInfo was called: no getUpdates, no deleteWebhook
            self.assertEqual(1, len(transport.requests))
            self.assertTrue(transport.last().url.endswith("/getWebhookInfo"))
        finally:
            conn.close()

    def test_offset_is_persisted_only_after_the_write_and_survives_a_second_poll(self):
        conn = _db()
        try:
            first = FakeTransport(_router(updates=_updates([_post_update(10), _post_update(11, message_id=101)])))
            report = poll_into_inbox(conn, first, bot_token=TOKEN, allowed_channels=[CHANNEL], base_url=BASE)
            self.assertTrue(report.polled)
            self.assertEqual(2, report.accepted)
            self.assertEqual(12, report.offset)
            self.assertEqual(12, stored_offset(conn))

            # a second poll passes the stored offset and receives nothing new
            second = FakeTransport(_router(updates=_updates([])))
            report2 = poll_into_inbox(conn, second, bot_token=TOKEN, allowed_channels=[CHANNEL], base_url=BASE)
            self.assertEqual("12", second.last().params["offset"])
            self.assertEqual(0, report2.received)
            self.assertEqual(12, report2.offset)
        finally:
            conn.close()

    def test_a_replayed_update_is_a_duplicate_not_a_second_row(self):
        conn = _db()
        try:
            transport = FakeTransport(_router(updates=_updates([_post_update(20)])))
            poll_into_inbox(conn, transport, bot_token=TOKEN, allowed_channels=[CHANNEL], base_url=BASE)
            # simulate the crash-between-the-two-writes case: the inbox row is on
            # disk but the offset never advanced
            conn.execute("DELETE FROM telegram_poll_state WHERE key = ?", (OFFSET_KEY,))
            again = FakeTransport(_router(updates=_updates([_post_update(20)])))
            report = poll_into_inbox(conn, again, bot_token=TOKEN, allowed_channels=[CHANNEL], base_url=BASE)
            self.assertEqual(1, report.duplicate)
            self.assertEqual(1, inbox_counts(conn)["messages"])
        finally:
            conn.close()


class CliTests(unittest.TestCase):
    def test_the_cli_action_polls_and_reports_counters(self):
        import contextlib
        import io
        import tempfile

        from randreport.connectors import transport as transport_module
        from randreport.ops import cli as ops_cli

        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        handle.close()
        db_path = handle.name
        seed = connect(db_path)
        apply_migrations(seed)
        seed.close()

        original = transport_module.UrllibTransport
        transport_module.UrllibTransport = lambda *a, **k: FakeTransport(
            _router(updates=_updates([_post_update(30)]))
        )
        env_backup = dict(os.environ)
        os.environ.update(
            {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_PUBLIC_CHANNEL": CHANNEL, "TELEGRAM_BASE_URL": BASE}
        )
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = ops_cli.main(["getupdates", "--db", db_path])
            self.assertEqual(0, code)
            payload = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertTrue(payload["polled"])
            self.assertEqual(1, payload["accepted"])
            self.assertFalse(payload["webhook"]["url_set"])
        finally:
            transport_module.UrllibTransport = original
            os.environ.clear()
            os.environ.update(env_backup)
            os.unlink(db_path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
