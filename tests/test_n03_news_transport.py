"""N03 — the Telegram post transport without a personal account.

Covers the card's transport clauses over ``docs/REQUIREMENTS.md`` section 8: a
webhook that passively and durably records public ``channel_post`` /
``edited_channel_post`` updates (secret, allowed channel, body limit, dedup), a
nightly read of the inbox with a bounded commit, an import path for a missed
public JSON export, an honest news gap, and a configurable LLM adapter that is
off (budget 0) by default and never accesses tools.

Everything is synthetic and local: a loopback WSGI server, invented posts, no
credential, no ``setWebhook`` call and no paid request.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from randreport.connectors.telegram import TelegramClient
from randreport.connectors.transport import FakeTransport, TransportError, json_response
from randreport.domain.timeframes import MSK
from randreport.inbox import import_json
from randreport.inbox.store import (
    MAX_BATCH,
    InboxPost,
    accept_post,
    commit_posts,
    inbox_counts,
    pending_posts,
)
from randreport.inbox.webhook import (
    SECRET_HEADER,
    WEBHOOK_PATH,
    TelegramWebhook,
    WebhookConfigError,
    parse_update,
)
from randreport.news.extract import NewsExtractor
from randreport.news.provider import (
    DEFAULT_MAX_CALLS_PER_DAY,
    ChatCompletionsProvider,
    LlmConfig,
    LlmConfigError,
    LlmRateLimited,
    config_from_env,
    provider_from_config,
)
from randreport.ops import cli as ops_cli
from randreport.ops.run import InboxAdapter, NewsAdapter, TelegramAdapter
from randreport.store import repository as repo
from tests import fixtures_ingest as ig

CHANNEL = "@random_culture"
FOREIGN = "someone_else"
CHANNEL_ID = -1001234567890
SECRET = "wh-secret-0123456789abcdef"
POST_TS = int(datetime(2025, 7, 12, 7, 0, tzinfo=timezone.utc).timestamp())
EDIT_TS = POST_TS + 3600
REFUSED = (401, 403, 404, 405, 413, 429)


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):  # noqa: D102 - keep the test output readable
        pass


class _Serving:
    """A WSGI application on loopback, closed in tearDown (the resource guard)."""

    def __init__(self, app) -> None:
        self.server: WSGIServer = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def request(base: str, path: str = WEBHOOK_PATH, *, body=None, headers=None, method: str = "POST"):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    http_request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    for name, value in (headers or {}).items():
        http_request.add_header(name, value)
    try:
        with urllib.request.urlopen(http_request, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8")
            return exc.code, dict(exc.headers or {}), json.loads(payload)
        finally:
            exc.close()


def secret_headers(value: str = SECRET) -> dict[str, str]:
    return {"X-Telegram-Bot-Api-Secret-Token": value}


def update(
    update_id: int,
    *,
    kind: str = "channel_post",
    message_id: int = 600,
    text: str = "Мы отремонтировали подвал.",
    date: int = POST_TS,
    username: str | None = "random_culture",
    edit_date: int | None = None,
    caption: str | None = None,
):
    chat: dict[str, object] = {"id": CHANNEL_ID, "type": "channel"}
    if username is not None:
        chat["username"] = username
    body: dict[str, object] = {"message_id": message_id, "date": date, "chat": chat}
    if caption is not None:
        body["caption"] = caption
    else:
        body["text"] = text
    if edit_date is not None:
        body["edit_date"] = edit_date
    return {"update_id": update_id, kind: body}


def inbox_rows(conn):
    return conn.execute("SELECT * FROM telegram_inbox ORDER BY update_id").fetchall()


class WebhookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        # the WSGI server answers each request in a worker thread: an injected
        # connection has to allow that (see randreport/store/db.connect)
        self.conn = ig.fresh_conn(check_same_thread=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def serve(self, **kwargs) -> _Serving:
        app = TelegramWebhook(
            secret_token=SECRET, allowed_channels=(CHANNEL,), conn=self.conn, **kwargs
        )
        serving = _Serving(app)
        self.addCleanup(serving.close)
        return serving


class TestWebhookIntake(WebhookTestCase):
    """HTTP → inbox: the secret, the allowlist, the body limit, dedup."""

    def test_a_public_channel_post_is_written_to_the_inbox_and_answered(self):
        serving = self.serve()
        status, _, payload = request(serving.base, body=update(1), headers=secret_headers())
        self.assertEqual(200, status)
        self.assertEqual("accepted", payload["status"])
        rows = inbox_rows(self.conn)
        self.assertEqual(1, len(rows))
        self.assertEqual("600", rows[0]["message_id"])
        self.assertEqual("@random_culture", rows[0]["channel_id"])
        self.assertIsNone(rows[0]["committed_at"])
        # what was received is on disk, not in memory: the reply follows the commit
        self.assertEqual(1, inbox_counts(self.conn)["pending_messages"])

    def test_a_wrong_or_missing_secret_is_refused_and_nothing_is_stored(self):
        serving = self.serve()
        for headers in (secret_headers("wh-secret-wrong-value-x"), {}):
            status, _, payload = request(serving.base, body=update(1), headers=headers)
            self.assertEqual(401, status)
            self.assertEqual("bad_secret", payload["error"])
        self.assertEqual((), tuple(inbox_rows(self.conn)))

    def test_the_application_refuses_to_start_without_a_secret(self):
        for value in (None, "", "short"):
            with self.assertRaises(WebhookConfigError):
                TelegramWebhook(secret_token=value, allowed_channels=(CHANNEL,), conn=self.conn)

    def test_a_post_from_another_channel_is_refused(self):
        serving = self.serve()
        status, _, payload = request(
            serving.base, body=update(1, username=FOREIGN), headers=secret_headers()
        )
        self.assertEqual(403, status)
        self.assertEqual("channel_not_allowed", payload["error"])
        self.assertEqual((), tuple(inbox_rows(self.conn)))

    def test_an_empty_allowlist_denies_everything(self):
        app = TelegramWebhook(secret_token=SECRET, allowed_channels=(), conn=self.conn)
        serving = _Serving(app)
        self.addCleanup(serving.close)
        status, _, _ = request(serving.base, body=update(1), headers=secret_headers())
        self.assertEqual(403, status)
        self.assertEqual((), tuple(inbox_rows(self.conn)))

    def test_a_body_over_the_limit_is_refused_before_it_is_parsed(self):
        serving = self.serve(max_body_bytes=512)
        status, _, payload = request(
            serving.base, body=update(1, text="я" * 4000), headers=secret_headers()
        )
        self.assertEqual(413, status)
        self.assertEqual("body_too_large", payload["error"])
        self.assertEqual((), tuple(inbox_rows(self.conn)))

    def test_a_replayed_update_is_idempotent(self):
        serving = self.serve()
        first = request(serving.base, body=update(7), headers=secret_headers())
        second = request(serving.base, body=update(7), headers=secret_headers())
        self.assertEqual(200, first[0])
        self.assertEqual(200, second[0])
        self.assertEqual("accepted", first[2]["status"])
        self.assertEqual("duplicate", second[2]["status"])
        self.assertEqual(1, len(inbox_rows(self.conn)))

    def test_an_edit_arrives_as_a_newer_version_of_the_same_message(self):
        serving = self.serve()
        request(serving.base, body=update(1, text="Первая версия."), headers=secret_headers())
        status, _, payload = request(
            serving.base,
            body=update(2, kind="edited_channel_post", text="Уточнённая версия.", edit_date=EDIT_TS),
            headers=secret_headers(),
        )
        self.assertEqual(200, status)
        self.assertEqual("revised", payload["status"])
        rows = inbox_rows(self.conn)
        self.assertEqual(2, len(rows))
        self.assertEqual([1, 2], [int(row["revision"]) for row in rows])
        # the nightly pass sees one post: the newest version
        pending = pending_posts(self.conn)
        self.assertEqual(1, len(pending))
        self.assertEqual("Уточнённая версия.", pending[0].text)

    def test_a_redelivery_of_an_older_version_does_not_overwrite_the_newer_one(self):
        serving = self.serve()
        request(serving.base, body=update(1, text="Первая версия."), headers=secret_headers())
        request(
            serving.base,
            body=update(2, kind="edited_channel_post", text="Уточнённая версия.", edit_date=EDIT_TS),
            headers=secret_headers(),
        )
        status, _, payload = request(serving.base, body=update(3, text="Первая версия."), headers=secret_headers())
        self.assertEqual(200, status)
        self.assertEqual("duplicate", payload["status"])
        self.assertEqual("Уточнённая версия.", pending_posts(self.conn)[0].text)

    def test_an_update_we_do_not_store_is_acknowledged_and_ignored(self):
        serving = self.serve()
        status, _, payload = request(
            serving.base,
            body={"update_id": 5, "message": {"message_id": 1, "date": POST_TS, "chat": {"id": 1}, "text": "личное"}},
            headers=secret_headers(),
        )
        # 200 so Telegram stops retrying something we will never store
        self.assertEqual(200, status)
        self.assertEqual("ignored", payload["status"])
        self.assertEqual((), tuple(inbox_rows(self.conn)))

    def test_a_photo_post_is_archived_from_its_caption(self):
        serving = self.serve()
        status, _, payload = request(
            serving.base, body=update(9, caption="Подвал готов."), headers=secret_headers()
        )
        self.assertEqual(200, status)
        self.assertEqual("accepted", payload["status"])
        self.assertEqual("Подвал готов.", pending_posts(self.conn)[0].text)

    def test_a_malformed_interesting_update_is_a_400(self):
        serving = self.serve()
        for body in (
            {"update_id": 0, "channel_post": {"message_id": 1, "date": POST_TS, "chat": {"id": 1}, "text": "x"}},
            {"update_id": 1, "channel_post": {"message_id": 1, "chat": {"id": 1}, "text": "x"}},
            {"update_id": 1, "channel_post": {"message_id": 1, "date": POST_TS, "text": "x"}},
            {"update_id": 1, "channel_post": {"message_id": 1, "date": POST_TS, "chat": {"id": 1}, "text": "  "}},
        ):
            status, _, payload = request(serving.base, body=body, headers=secret_headers())
            self.assertEqual(400, status, body)
            self.assertEqual("invalid_update", payload["error"])
        self.assertEqual((), tuple(inbox_rows(self.conn)))

    def test_a_non_json_body_is_a_400(self):
        serving = self.serve()
        status, _, payload = request(serving.base, body=b"not json", headers=secret_headers())
        self.assertEqual(400, status)
        self.assertEqual("invalid_json", payload["error"])

    def test_the_rate_limit_answers_429_with_retry_after(self):
        serving = self.serve(max_updates_per_minute=2)
        self.assertEqual(200, request(serving.base, body=update(1), headers=secret_headers())[0])
        self.assertEqual(200, request(serving.base, body=update(2), headers=secret_headers())[0])
        status, headers, payload = request(serving.base, body=update(3), headers=secret_headers())
        self.assertEqual(429, status)
        self.assertEqual("rate_limited", payload["error"])
        self.assertIn("Retry-After", headers)
        self.assertEqual(2, len(inbox_rows(self.conn)))

    def test_only_the_webhook_path_and_post_are_served(self):
        serving = self.serve()
        self.assertEqual(404, request(serving.base, path="/", body=update(1), headers=secret_headers())[0])
        self.assertEqual(
            405, request(serving.base, body=update(1), headers=secret_headers(), method="GET")[0]
        )
        self.assertEqual((), tuple(inbox_rows(self.conn)))

    def test_the_secret_never_leaves_the_process(self):
        serving = self.serve()
        status, headers, payload = request(serving.base, body=update(1), headers=secret_headers())
        self.assertEqual(200, status)
        blob = json.dumps({"headers": headers, "payload": payload}) + repr(
            TelegramWebhook(secret_token=SECRET, allowed_channels=(CHANNEL,), conn=self.conn)
        )
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("X-Telegram", blob)

    def test_no_source_except_the_inbox_path_accepts_an_update(self):
        # nothing enumerates or polls for updates: no /setWebhook call and no
        # getUpdates poll — the webhook is served, never registered
        from randreport.inbox import store as inbox_store
        from randreport.inbox import webhook as inbox_webhook

        for module in (inbox_store, inbox_webhook, import_json):
            source = Path(str(module.__file__)).read_text(encoding="utf-8")
            self.assertNotIn("/setWebhook", source)
            self.assertNotIn("getUpdates", source)
            self.assertNotIn("api.telegram.org", source)
        cli_source = Path(str(ops_cli.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("/setWebhook", cli_source)


class TestWebhookToNightly(WebhookTestCase):
    """HTTP → inbox → nightly: the commit, the archive, the honest gap."""

    def adapters(self, *, history_verified=False, provider=None, script=None):
        extractor = NewsExtractor(
            self.conn,
            provider=provider,
            channel_username="random_culture",
            max_calls_per_date=10,
        )
        if script is not None:
            extractor = ig.extractor(self.conn, script=script, max_calls_per_date=10)
        return [
            InboxAdapter(history_verified=history_verified),
            NewsAdapter(extractor, history_verified=history_verified),
        ]

    def test_a_post_received_by_http_reaches_the_archive_in_the_nightly_cycle(self):
        serving = self.serve()
        request(serving.base, body=update(1, text="Мы отремонтировали подвал."), headers=secret_headers())
        report = ig.run_once(self.conn, self.adapters(), lease=self.lease)

        archived = repo.archived_posts(self.conn)
        self.assertEqual(1, len(archived))
        self.assertEqual("Мы отремонтировали подвал.", archived[0].text)
        self.assertEqual("@random_culture", archived[0].channel_id)
        self.assertEqual(0, inbox_counts(self.conn)["pending_messages"])
        self.assertEqual("ok", report.sources["telegram_posts"].status)
        self.assertTrue(report.published)

    def test_the_nightly_read_is_idempotent(self):
        serving = self.serve()
        request(serving.base, body=update(1), headers=secret_headers())
        ig.run_once(self.conn, self.adapters(), lease=self.lease)
        second = ig.run_once(self.conn, self.adapters(), lease=self.lease)
        self.assertEqual(1, len(repo.archived_posts(self.conn)))
        self.assertEqual(0, second.sources["telegram_posts"].payload["archive_new"])
        self.assertEqual(0, second.sources["telegram_posts"].payload["pending_messages"])

    def test_an_edited_post_arrives_in_the_archive_once_with_the_new_text(self):
        serving = self.serve()
        request(serving.base, body=update(1, text="Первая версия."), headers=secret_headers())
        request(
            serving.base,
            body=update(2, kind="edited_channel_post", text="Уточнённая версия.", edit_date=EDIT_TS),
            headers=secret_headers(),
        )
        ig.run_once(self.conn, self.adapters(), lease=self.lease)
        archived = repo.archived_posts(self.conn)
        self.assertEqual(1, len(archived))
        self.assertEqual("Уточнённая версия.", archived[0].text)
        self.assertIsNotNone(archived[0].edited_at)

    def test_the_commit_is_bounded_at_two_hundred_messages_per_pass(self):
        stamp = datetime(2025, 7, 12, 10, 0, tzinfo=MSK)
        for index in range(MAX_BATCH + 5):
            accept_post(
                self.conn,
                InboxPost(
                    update_id=str(1000 + index),
                    kind="channel_post",
                    channel_id=CHANNEL,
                    message_id=str(600 + index),
                    posted_at=stamp + timedelta(minutes=index),
                    text=f"пост {index}",
                ),
            )
        report = ig.run_once(self.conn, [InboxAdapter()], lease=self.lease)
        payload = report.sources["telegram_posts"].payload
        self.assertEqual(MAX_BATCH, payload["committed_messages"])
        self.assertEqual(5, payload["pending_messages"])
        self.assertEqual(MAX_BATCH, len(repo.archived_posts(self.conn)))
        self.assertIn("news_inbox_backlog", {issue["code"] for issue in repo.issues(self.conn)})

    def test_an_empty_inbox_is_a_gap_and_never_a_complete_news_section(self):
        report = ig.run_once(self.conn, self.adapters(), lease=self.lease)
        news = report.sources["news"]
        self.assertEqual("ok", news.status)
        self.assertEqual("partial", news.coverage)
        self.assertIn("no_posts_received", news.payload["coverage_reasons"])
        self.assertIn("history_gap_possible", news.payload["coverage_reasons"])
        release = json.loads(repo.current_release(self.conn)["payload"])
        self.assertEqual("partial", release["chronicle"]["completeness"])
        self.assertNotEqual("complete", release["chronicle"]["completeness"])

    def test_the_inbox_alone_never_claims_complete_coverage(self):
        report = ig.run_once(self.conn, self.adapters(), lease=self.lease)
        self.assertEqual("partial", report.sources["telegram_posts"].coverage)
        self.assertIn(
            "inbox_cannot_prove_completeness",
            report.sources["telegram_posts"].payload["coverage_reasons"],
        )

    def test_a_verified_history_and_a_provider_make_the_section_complete(self):
        serving = self.serve()
        request(serving.base, body=update(1), headers=secret_headers())
        script = [ig.news_response([ig.news_item(message_ids=("600",))])]
        report = ig.run_once(
            self.conn, self.adapters(history_verified=True, script=script), lease=self.lease
        )
        self.assertEqual("complete", report.sources["news"].coverage)
        self.assertEqual("complete", report.sources["telegram_posts"].coverage)
        release = json.loads(repo.current_release(self.conn)["payload"])
        self.assertEqual("complete", release["chronicle"]["completeness"])
        self.assertEqual(1, release["chronicle"]["metrics"]["news_count"])
        # the link is built from the archived message id, never from the model
        self.assertEqual(["https://t.me/random_culture/600"], release["chronicle"]["news_items"][0]["source_urls"])

    def test_a_provider_without_a_budget_spends_nothing_and_says_so(self):
        serving = self.serve()
        request(serving.base, body=update(1), headers=secret_headers())
        configured = LlmConfig(
            provider="http",
            base_url="https://provider.invalid",
            model="cheap-1",
            max_calls_per_day=0,
        )
        provider = provider_from_config(FakeTransport(), configured, api_key="not-a-real-key")
        extractor = NewsExtractor(
            self.conn,
            provider=provider,
            channel_username="random_culture",
            max_calls_per_date=configured.max_calls_per_day,
            max_tokens_per_date=configured.max_tokens_per_day,
        )
        report = ig.run_once(self.conn, [InboxAdapter(), NewsAdapter(extractor)], lease=self.lease)
        self.assertTrue(report.sources["news"].payload["deferred"])
        self.assertEqual(0, report.sources["news"].payload["calls"])
        self.assertIn("llm_budget_exceeded", {issue["code"] for issue in repo.issues(self.conn)})
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()["n"])

    def test_a_partial_inbox_does_not_stop_the_other_sections(self):
        reports = [ig.telegram(posts=[], members=4128)]
        rows = [ig.zen_row("t1", "2025-07-05", 100_000, account="acc_card", tag=["cat_rent"])]
        client, _ = ig.zenmoney(rows=rows, server_timestamp=1000)
        adapters = [
            ig.zenmoney_adapter(client),
            TelegramAdapter(reports[0][0], chat_id=CHANNEL, channel_id=CHANNEL),
            InboxAdapter(),
            NewsAdapter(
                NewsExtractor(self.conn, provider=None, channel_username="random_culture"),
                history_verified=False,
            ),
        ]
        report = ig.run_once(self.conn, adapters, lease=self.lease)
        self.assertTrue(report.published)
        release = json.loads(repo.current_release(self.conn)["payload"])
        self.assertEqual(1, release["community"]["metrics"]["points"])
        self.assertIsNotNone(release["money"]["metrics"]["expense_kopecks"])
        self.assertEqual("partial", release["chronicle"]["completeness"])


class TestImportMissedPublicJson(WebhookTestCase):
    """§8 backfill: a missed public export is imported, never invented."""

    def export(self):
        return {
            "name": "Публичный канал РанДома",
            "messages": [
                {"id": 700, "date": "2025-07-14T10:00:00+03:00", "text": "Починили крышу."},
                {"id": 701, "date": "2025-07-15T10:00:00+03:00", "text": ["Купили", " стеллажи."]},
            ],
        }

    def test_a_missed_export_is_imported_into_the_archive(self):
        report = import_json.import_public_posts(self.conn, self.export(), channel_id=CHANNEL)
        self.assertEqual(2, report.imported)
        self.assertEqual(0, report.rejected)
        archived = {post.message_id: post.text for post in repo.archived_posts(self.conn)}
        self.assertEqual("Починили крышу.", archived["700"])
        self.assertEqual("Купили стеллажи.", archived["701"])

    def test_a_repeated_import_does_not_duplicate_a_post(self):
        import_json.import_public_posts(self.conn, self.export(), channel_id=CHANNEL)
        second = import_json.import_public_posts(self.conn, self.export(), channel_id=CHANNEL)
        self.assertEqual(0, second.imported)
        self.assertEqual(2, second.unchanged)
        self.assertEqual(2, len(repo.archived_posts(self.conn)))

    def test_unreadable_entries_are_counted_and_not_invented(self):
        payload = {
            "messages": [
                {"id": 700, "date": "2025-07-14T10:00:00+03:00", "text": "Починили крышу."},
                {"id": "abc", "date": "2025-07-14T10:00:00+03:00", "text": "плохой id"},
                {"id": 702, "date": "завтра", "text": "плохая дата"},
                {"id": 703, "date": "2025-07-14T10:00:00+03:00", "text": "   "},
                "не объект",
            ]
        }
        report = import_json.import_public_posts(self.conn, payload, channel_id=CHANNEL)
        self.assertEqual(1, report.imported)
        self.assertEqual(4, report.rejected)
        self.assertEqual(1, len(repo.archived_posts(self.conn)))

    def test_a_telegram_desktop_shape_with_unix_time_is_accepted(self):
        payload = [{"id": 800, "date_unixtime": str(POST_TS), "text": "Импорт по времени."}]
        report = import_json.import_public_posts(self.conn, payload, channel_id=CHANNEL)
        self.assertEqual(1, report.imported)
        self.assertEqual("Импорт по времени.", repo.archived_posts(self.conn)[0].text)

    def test_a_payload_that_is_not_an_export_is_refused(self):
        with self.assertRaises(import_json.ImportRejected):
            import_json.import_public_posts(self.conn, "не экспорт", channel_id=CHANNEL)
        with self.assertRaises(import_json.ImportRejected):
            import_json.import_public_posts(self.conn, {"messages": {"id": 1}}, channel_id=CHANNEL)

    def test_the_import_never_touches_money_and_is_recorded(self):
        rows = [ig.zen_row("t1", "2025-07-05", 100_000, account="acc_card", tag=["cat_rent"])]
        client, _ = ig.zenmoney(rows=rows, server_timestamp=1000)
        ig.run_once(self.conn, [ig.zenmoney_adapter(client)], lease=self.lease)
        before = json.loads(repo.current_release(self.conn)["payload"])["money"]

        import_json.import_public_posts(self.conn, self.export(), channel_id=CHANNEL)

        after = json.loads(repo.current_release(self.conn)["payload"])["money"]
        self.assertEqual(before, after)
        imported = self.conn.execute("SELECT * FROM news_imports").fetchall()
        self.assertEqual(1, len(imported))
        self.assertEqual(CHANNEL, imported[0]["channel_id"])
        self.assertEqual(2, int(imported[0]["imported"]))
        self.assertIn("news_import", {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")})

    def test_an_imported_post_reaches_the_public_chronicle_through_the_nightly_cycle(self):
        import_json.import_public_posts(self.conn, self.export(), channel_id=CHANNEL)
        script = [ig.news_response([ig.news_item(message_ids=("700",), title="Починили крышу")])]
        extractor = ig.extractor(self.conn, script=script)
        report = ig.run_once(
            self.conn, [InboxAdapter(history_verified=True), NewsAdapter(extractor, history_verified=True)],
            lease=self.lease,
        )
        release = json.loads(repo.current_release(self.conn)["payload"])
        self.assertEqual(1, release["chronicle"]["metrics"]["news_count"])
        self.assertEqual("complete", report.sources["news"].coverage)


class TestLlmAdapter(WebhookTestCase):
    """The configurable, off-by-default adapter: budget, cache, 429, timeout."""

    def chat_app(self, script):
        """A loopback chat-completions endpoint; records every request it sees."""

        state = {"queue": list(script), "requests": []}

        def app(environ, start_response):
            length = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(length) if length else b""
            state["requests"].append(
                {
                    "path": environ.get("PATH_INFO"),
                    "authorization": environ.get("HTTP_AUTHORIZATION"),
                    "body": json.loads(raw.decode("utf-8")) if raw else None,
                }
            )
            status, payload = state["queue"].pop(0) if state["queue"] else (200, {"choices": []})
            body = json.dumps(payload).encode("utf-8")
            start_response(f"{status} x", [("Content-Type", "application/json")])
            return [body]

        return app, state

    def provider_for(self, serving, *, model="cheap-1", budget=5):
        config = LlmConfig(
            provider="http",
            base_url=serving.base,
            model=model,
            max_calls_per_day=budget,
            max_tokens_per_day=100_000,
        )
        transport = __import__("randreport.connectors.transport", fromlist=["UrllibTransport"]).UrllibTransport()
        return provider_from_config(transport, config, api_key="synthetic-key-1234"), config

    def test_the_default_configuration_is_off_with_a_budget_of_zero(self):
        config = config_from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(0, config.max_calls_per_day)
        self.assertEqual(0, config.max_tokens_per_day)
        self.assertEqual(DEFAULT_MAX_CALLS_PER_DAY, config.max_calls_per_day)
        self.assertIsNone(provider_from_config(FakeTransport(), config))

    def test_a_selected_provider_without_a_budget_is_still_off(self):
        config = config_from_env({"NEWS_LLM_PROVIDER": "http", "NEWS_LLM_BASE_URL": "https://x.invalid", "NEWS_LLM_MODEL": "m"})
        self.assertTrue(config.enabled)
        self.assertFalse(config.budget)
        self.assertEqual(0, config.max_calls_per_day)

    def test_a_provider_without_an_endpoint_is_refused(self):
        with self.assertRaises(LlmConfigError):
            provider_from_config(FakeTransport(), LlmConfig(provider="http", model="m"))
        with self.assertRaises(LlmConfigError):
            provider_from_config(FakeTransport(), LlmConfig(provider="http", base_url="https://x.invalid"))
        with self.assertRaises(LlmConfigError):
            config_from_env({"NEWS_LLM_MAX_CALLS_PER_DAY": "много"})

    def test_a_configured_provider_extracts_news_over_real_http(self):
        answer = json.dumps({"items": [ig.news_item(message_ids=("600",), title="Новые стеллажи")]})
        app, state = self.chat_app([(200, {"choices": [{"message": {"content": answer}}]})])
        serving = _Serving(app)
        self.addCleanup(serving.close)
        provider, _ = self.provider_for(serving)

        repo.upsert_posts(self.conn, [ig.post("600", "Поставили новые стеллажи.")])
        extractor = NewsExtractor(self.conn, provider=provider, channel_username="random_culture", max_calls_per_date=5)
        outcome = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")

        self.assertEqual(1, outcome.published)
        self.assertEqual(1, len(state["requests"]))
        call = state["requests"][0]
        self.assertEqual("/v1/chat/completions", call["path"])
        # the key travels in a header; the URL carries no secret
        self.assertEqual("Bearer synthetic-key-1234", call["authorization"])
        # the model gets no tools and no database: exactly these three keys
        self.assertEqual({"model", "temperature", "messages"}, set(call["body"]))
        self.assertEqual("cheap-1", call["body"]["model"])
        self.assertIn("<untrusted_posts>", call["body"]["messages"][1]["content"])

    def test_a_rate_limited_provider_defers_instead_of_retrying(self):
        app, state = self.chat_app([(429, {"error": "slow down"})])
        serving = _Serving(app)
        self.addCleanup(serving.close)
        provider, _ = self.provider_for(serving)
        repo.upsert_posts(self.conn, [ig.post("600", "Пост.")])
        extractor = NewsExtractor(self.conn, provider=provider, channel_username="random_culture", max_calls_per_date=5)

        outcome = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")
        self.assertTrue(outcome.deferred)
        self.assertTrue(outcome.rate_limited)
        self.assertEqual(1, len(state["requests"]))  # one attempt, no retry storm
        self.assertIn("llm_rate_limited", {issue["code"] for issue in repo.issues(self.conn)})
        deferred = self.conn.execute("SELECT reason FROM deferred_jobs").fetchall()
        self.assertEqual(["rate_limited"], [row["reason"] for row in deferred])
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()["n"])

    def test_a_timeout_is_bounded_and_then_degrades(self):
        calls = {"n": 0}

        class TimingOut:
            model = "slow"

            def complete(self, *, system, prompt, timeout_s):
                calls["n"] += 1
                raise TransportError("timed out")

        repo.upsert_posts(self.conn, [ig.post("600", "Пост.")])
        extractor = NewsExtractor(
            self.conn, provider=TimingOut(), channel_username="random_culture", retries=2, max_calls_per_date=5,
            sleep=lambda _seconds: None,
        )
        outcome = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")
        self.assertTrue(outcome.degraded)
        self.assertEqual(3, calls["n"])  # bounded retries, then give up
        self.assertIn("llm_error", {issue["code"] for issue in repo.issues(self.conn)})

    def test_the_cache_prevents_a_second_paid_call(self):
        answer = json.dumps({"items": [ig.news_item(message_ids=("600",))]})
        app, state = self.chat_app([(200, {"choices": [{"message": {"content": answer}}]})])
        serving = _Serving(app)
        self.addCleanup(serving.close)
        provider, _ = self.provider_for(serving)
        repo.upsert_posts(self.conn, [ig.post("600", "Пост.")])
        extractor = NewsExtractor(self.conn, provider=provider, channel_username="random_culture", max_calls_per_date=5)

        first = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")
        second = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(1, len(state["requests"]))

    def test_the_daily_budget_caps_the_number_of_calls(self):
        answer = json.dumps({"items": [ig.news_item(message_ids=("600",))]})
        app, state = self.chat_app([(200, {"choices": [{"message": {"content": answer}}]})])
        serving = _Serving(app)
        self.addCleanup(serving.close)
        provider, _ = self.provider_for(serving, budget=1)
        repo.upsert_posts(self.conn, [ig.post("600", "Первый пост.")])
        extractor = NewsExtractor(self.conn, provider=provider, channel_username="random_culture", max_calls_per_date=1)

        first = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")
        self.assertEqual(1, first.calls)
        repo.upsert_posts(self.conn, [ig.post("601", "Второй пост.")])
        second = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")
        self.assertTrue(second.deferred)
        self.assertIn("llm_budget_exceeded", {issue["code"] for issue in repo.issues(self.conn)})
        self.assertEqual(1, len(state["requests"]))
        ledger = self.conn.execute("SELECT calls FROM llm_budget_ledger").fetchone()
        self.assertEqual(1, int(ledger["calls"]))

    def test_a_zero_token_budget_does_not_block_a_positive_call_budget(self):
        answer = json.dumps({"items": [ig.news_item(message_ids=("600",))]})
        app, state = self.chat_app([(200, {"choices": [{"message": {"content": answer}}]})])
        serving = _Serving(app)
        self.addCleanup(serving.close)
        provider, _ = self.provider_for(serving)
        repo.upsert_posts(self.conn, [ig.post("600", "Пост.")])
        extractor = NewsExtractor(
            self.conn, provider=provider, channel_username="random_culture",
            max_calls_per_date=5, max_tokens_per_date=0,
        )
        outcome = extractor.run(repo.archived_posts(self.conn), logical_date="2025-07-31")
        # the call budget is the gate; a 0 token budget means "no separate cap"
        self.assertFalse(outcome.deferred)
        self.assertEqual(1, outcome.calls)
        self.assertEqual(1, len(state["requests"]))

    def test_the_http_provider_maps_statuses_to_typed_errors(self):
        provider = ChatCompletionsProvider(
            FakeTransport([json_response({"error": "nope"}, status=429)]),
            base_url="https://provider.invalid",
            model="cheap-1",
            api_key="synthetic-key-1234",
        )
        with self.assertRaises(LlmRateLimited):
            provider.complete(system="s", prompt="p", timeout_s=1)
        self.assertNotIn("synthetic-key-1234", repr(provider))


class TestDeferredJobsConsumer(WebhookTestCase):
    """Option (б): deferred LLM batches are retried at news-sync start.

    The extractor writes one ``deferred_jobs`` row per batch it could not run
    (rate limit, exhausted budget); the sync reads them back, re-runs the batch
    under the original logical date and clears the row on success.  The night
    cursor belongs to the regular pending pass and is not moved by a retry.
    """

    def seed_row(
        self, *, job_id: str, ids: Sequence[str], reason: str = "rate_limited",
        created_at: str = "2025-07-31T00:00:00+00:00",
    ) -> None:
        self.conn.execute(
            "INSERT INTO deferred_jobs (job_id, kind, payload, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, "news_extraction", json.dumps(list(ids)), reason, created_at),
        )
        self.conn.commit()

    def test_a_successful_retry_clears_the_row_and_keeps_the_cursor(self):
        answer = json.dumps({"items": [ig.news_item(message_ids=("600",), title="Отложенные новости")]})
        extractor = ig.extractor(self.conn, script=[answer], max_calls_per_date=5)
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2025-07-30", ids=["600"])
        repo.upsert_posts(self.conn, [ig.post("600", "Пост из отложенной партии.")])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="600")

        rows = self.conn.execute("SELECT * FROM deferred_jobs").fetchall()
        self.assertEqual([], rows)
        self.assertEqual(1, len(getattr(extractor.provider, "calls", [])))
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()["n"])
        # the retry is charged to the deferred day, not to tonight's date
        ledger = self.conn.execute("SELECT logical_date FROM llm_budget_ledger").fetchone()
        self.assertEqual("2025-07-30", ledger["logical_date"])
        # the night cursor belongs to the pending pass: nothing newer was seen
        self.assertEqual("600", outcome.cursor)
        self.assertEqual(1, outcome.payload["deferred_jobs_retried"])
        self.assertEqual(1, outcome.payload["deferred_jobs_cleared"])
        self.assertEqual(1, outcome.payload["posts"])

    def test_a_retry_that_is_rate_limited_again_leaves_the_row(self):
        extractor = ig.extractor(self.conn, script=[LlmRateLimited("429")], max_calls_per_date=5)
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2025-07-30", ids=["600"])
        repo.upsert_posts(self.conn, [ig.post("600", "Пост.")])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="600")

        deferred = self.conn.execute("SELECT job_id, reason FROM deferred_jobs").fetchall()
        self.assertEqual(["news_2025-07-30"], [row["job_id"] for row in deferred])
        self.assertEqual(["rate_limited"], [row["reason"] for row in deferred])
        self.assertTrue(outcome.payload["deferred"])  # the retry's deferral is honest coverage
        self.assertEqual(1, outcome.payload["deferred_jobs_retried"])
        self.assertEqual(0, outcome.payload["deferred_jobs_cleared"])

    def test_a_row_without_any_live_post_is_cleared_without_a_provider_call(self):
        extractor = ig.extractor(self.conn, script=[], max_calls_per_date=5)
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2025-07-30", ids=["600"])
        repo.upsert_posts(self.conn, [ig.post("600", "Пост.", deleted=True)])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="600")

        rows = self.conn.execute("SELECT * FROM deferred_jobs").fetchall()
        self.assertEqual([], rows)
        self.assertEqual([], getattr(extractor.provider, "calls", []))  # nothing left to extract
        self.assertEqual(0, outcome.payload["deferred_jobs_retried"])
        self.assertEqual(1, outcome.payload["deferred_jobs_cleared"])

    def test_without_a_provider_nothing_is_retried_and_nothing_is_cleared(self):
        extractor = NewsExtractor(self.conn, provider=None, channel_username="random_culture")
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2025-07-30", ids=["600"])
        repo.upsert_posts(self.conn, [ig.post("600", "Пост.")])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="600")

        deferred = self.conn.execute("SELECT job_id FROM deferred_jobs").fetchall()
        self.assertEqual(["news_2025-07-30"], [row["job_id"] for row in deferred])
        self.assertEqual(0, outcome.payload["deferred_jobs_retried"])

    def test_posts_deleted_since_the_deferral_are_dropped_from_the_retry(self):
        answer = json.dumps({"items": [ig.news_item(message_ids=("600",), title="Оставшиеся новости")]})
        extractor = ig.extractor(self.conn, script=[answer], max_calls_per_date=5)
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2025-07-30", ids=["600", "601"])
        repo.upsert_posts(self.conn, [
            ig.post("600", "Пост из отложенной партии."),
            ig.post("601", "Удалённый пост.", deleted=True),
        ])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="601")

        rows = self.conn.execute("SELECT * FROM deferred_jobs").fetchall()
        self.assertEqual([], rows)
        self.assertEqual(1, len(getattr(extractor.provider, "calls", [])))
        prompt = extractor.provider.calls[0]["prompt"]
        self.assertIn("Пост из отложенной партии.", prompt)
        self.assertNotIn("Удалённый пост.", prompt)
        self.assertEqual(1, outcome.payload["deferred_jobs_retried"])
        self.assertEqual(1, outcome.payload["deferred_jobs_cleared"])

    def test_an_exhausted_budget_bounds_the_retry_loop_and_defers_again(self):
        # no attempt counter and no TTL: the loop is bounded by the budget gate
        extractor = ig.extractor(self.conn, script=[], max_calls_per_date=0)
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2025-07-30", ids=["600"])
        repo.upsert_posts(self.conn, [ig.post("600", "Пост.")])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="600")

        deferred = self.conn.execute("SELECT job_id, reason FROM deferred_jobs").fetchall()
        self.assertEqual(["news_2025-07-30"], [row["job_id"] for row in deferred])
        self.assertEqual(["budget_exceeded"], [row["reason"] for row in deferred])
        self.assertEqual([], getattr(extractor.provider, "calls", []))  # the gate fires before any request
        self.assertTrue(outcome.payload["deferred"])
        self.assertEqual(1, outcome.payload["deferred_jobs_retried"])
        self.assertEqual(0, outcome.payload["deferred_jobs_cleared"])

    def test_an_old_row_is_still_consumed_there_is_no_ttl(self):
        first = json.dumps({"items": [ig.news_item(message_ids=("500",), title="Старая партия")]})
        second = json.dumps({"items": [ig.news_item(message_ids=("600",), title="Свежая партия")]})
        extractor = ig.extractor(self.conn, script=[first, second], max_calls_per_date=5)
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2025-07-29", ids=["500"], created_at="2020-01-01T00:00:00+00:00")
        self.seed_row(job_id="news_2025-07-30", ids=["600"])
        repo.upsert_posts(self.conn, [ig.post("500", "Старый пост."), ig.post("600", "Свежий пост.")])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="600")

        # a row of any age is consumed, never silently dropped: no row stays
        # "forever" without a consumer (the acceptance clause of DECISIONS.md)
        self.assertEqual([], self.conn.execute("SELECT * FROM deferred_jobs").fetchall())
        prompts = [call["prompt"] for call in extractor.provider.calls]
        self.assertIn("Старый пост.", prompts[0])
        self.assertNotIn("Свежий пост.", prompts[0])  # oldest batch first
        self.assertEqual(2, outcome.payload["deferred_jobs_retried"])
        self.assertEqual(2, outcome.payload["deferred_jobs_cleared"])

    def test_the_prod_orphan_row_is_consumed_by_a_plain_sync_without_a_migration(self):
        # the exact shape of the row the deploy inherited (prod, 28.11.2026):
        # job_id news_2026-11-28, reason rate_limited, payload ["4", "1622"]
        answer = json.dumps({"items": [ig.news_item(message_ids=("4", "1622"), title="Партия 28 ноября")]})
        extractor = ig.extractor(self.conn, script=[answer], max_calls_per_date=5)
        adapter = ig.news_adapter(extractor)
        self.seed_row(job_id="news_2026-11-28", ids=["4", "1622"])
        repo.upsert_posts(self.conn, [ig.post("4", "Первый пост партии."), ig.post("1622", "Второй пост партии.")])

        outcome = adapter.sync(conn=self.conn, cutoff=ig.NOW, logical_date="2025-08-01", cursor="1622")

        self.assertEqual([], self.conn.execute("SELECT * FROM deferred_jobs").fetchall())
        ledger = self.conn.execute("SELECT logical_date FROM llm_budget_ledger").fetchall()
        self.assertEqual(["2026-11-28"], [row["logical_date"] for row in ledger])  # charged to the deferred day
        self.assertEqual(2, outcome.payload["posts"])
        self.assertEqual(1, outcome.payload["deferred_jobs_retried"])
        self.assertEqual(1, outcome.payload["deferred_jobs_cleared"])


class TestSnapshotApartFromHistory(WebhookTestCase):
    """The subscriber snapshot and the post history are separate concerns."""

    def test_the_snapshot_is_recorded_when_no_history_source_is_configured(self):
        client = TelegramClient(
            ig.RecordingTransport(lambda request: json_response({"ok": True, "result": 4128})),
            bot_token="bot-token",
            history=None,
            allowed_channel_ids=(CHANNEL,),
        )
        report = ig.run_once(
            self.conn, [TelegramAdapter(client, chat_id=CHANNEL, channel_id=CHANNEL)], lease=self.lease
        )
        # the point is recorded; the source is not "partial" because of the history
        self.assertEqual("ok", report.sources["telegram"].status)
        points = repo.channel_points(self.conn)
        self.assertEqual(1, len(points))
        self.assertEqual(4128, points[0][1])
        # an absent history source is a documented state, not a nightly error:
        # no issue to nag every night, the gap travels in the coverage/payload
        self.assertEqual("not_configured", report.sources["telegram"].payload["history"])
        self.assertIsNone(report.sources["telegram"].payload["history_error"])
        self.assertNotIn("telegram_history_unavailable", {issue["code"] for issue in repo.issues(self.conn)})

    def test_a_snapshot_error_does_not_become_a_news_failure(self):
        client, _ = ig.telegram(posts=[], members=TransportError("bot api down"))
        extractor = NewsExtractor(self.conn, provider=None, channel_username="random_culture")
        report = ig.run_once(
            self.conn,
            [
                TelegramAdapter(client, chat_id=CHANNEL, channel_id=CHANNEL),
                NewsAdapter(extractor),
            ],
            lease=self.lease,
        )
        self.assertEqual("partial", report.sources["telegram"].status)
        self.assertEqual("ok", report.sources["news"].status)
        self.assertIn("telegram_snapshot_missing", {issue["code"] for issue in repo.issues(self.conn)})

    def test_a_failed_history_is_not_counted_as_a_snapshot_error(self):
        def handler(request):
            if "getChatMemberCount" in request.url:
                return json_response({"ok": True, "result": 4200})
            return json_response({"ok": True, "result": []})

        class BrokenHistory:
            def fetch(self, *, since_id, limit):
                raise RuntimeError("MTProto session expired")

        client = TelegramClient(
            ig.RecordingTransport(handler),
            bot_token="bot-token",
            history=BrokenHistory(),
            allowed_channel_ids=(CHANNEL,),
        )
        report = ig.run_once(
            self.conn, [TelegramAdapter(client, chat_id=CHANNEL, channel_id=CHANNEL)], lease=self.lease
        )
        self.assertEqual("ok", report.sources["telegram"].status)
        self.assertEqual(4200, repo.channel_points(self.conn)[0][1])
        codes = {issue["code"] for issue in repo.issues(self.conn)}
        self.assertIn("telegram_history_unavailable", codes)
        self.assertNotIn("telegram_snapshot_missing", codes)

    def test_the_status_action_reports_the_inbox_without_leaking_a_secret(self):
        import contextlib
        import io
        import tempfile
        from unittest import mock

        from randreport.store import connect as store_connect

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "status.sqlite")
            conn = store_connect(db_path, check_same_thread=False)
            app = TelegramWebhook(secret_token=SECRET, allowed_channels=(CHANNEL,), conn=conn)
            serving = _Serving(app)
            try:
                request(serving.base, body=update(1), headers=secret_headers())
            finally:
                serving.close()
                conn.close()

            buffer = io.StringIO()
            with mock.patch.dict(os.environ, {"RANREPORT_DB": db_path}, clear=False):
                with contextlib.redirect_stdout(buffer):
                    code = ops_cli.main(["status"])

        self.assertEqual(0, code)
        output = buffer.getvalue()
        self.assertIn('"source": "telegram_posts"', output)
        self.assertIn("inbox_counts", output)
        self.assertIn('"pending_messages": 1', output)
        self.assertNotIn(SECRET, output)


class TestWebhookCommand(unittest.TestCase):
    """The CLI wiring of the webhook service: refuse without a secret."""

    def test_the_cli_refuses_to_serve_without_a_secret(self):
        import contextlib
        import io
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "webhook.sqlite")
            buffer, errors = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "", "TELEGRAM_PUBLIC_CHANNEL": CHANNEL}, clear=False):
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
                    code = ops_cli.main(["webhook", "--db", db_path, "--port", "0"])
        self.assertEqual(2, code)
        self.assertIn("webhook not started", errors.getvalue())
        self.assertNotIn(SECRET, errors.getvalue())

    def test_the_webhook_app_is_built_from_the_environment(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "webhook.sqlite")
            self.assertIsNone(ops_cli.build_webhook_app(db_path, env={}))
            self.assertIsNone(
                ops_cli.build_webhook_app(db_path, env={"TELEGRAM_WEBHOOK_SECRET": "short"})
            )
            app = ops_cli.build_webhook_app(
                db_path,
                env={"TELEGRAM_WEBHOOK_SECRET": SECRET, "TELEGRAM_PUBLIC_CHANNEL": f"{CHANNEL}, @second"},
            )
            assert app is not None
            self.assertEqual(WEBHOOK_PATH, app.path)
            self.assertEqual(frozenset({CHANNEL, "@second"}), app.allowed_channels)
            app.close()


class TestInboxUnit(unittest.TestCase):
    """The inbox primitives on their own."""

    def setUp(self) -> None:
        self.conn = ig.fresh_conn()
        self.addCleanup(self.conn.close)

    def post(self, update_id: str, message_id: str = "600", text: str = "текст", *, revision: int | None = None):
        moment = datetime(2025, 7, 12, 7, 0, tzinfo=timezone.utc)
        return InboxPost(
            update_id=update_id,
            kind="channel_post",
            channel_id=CHANNEL,
            message_id=message_id,
            posted_at=moment,
            text=text,
        )

    def test_a_post_needs_a_kind_a_time_zone_and_a_bounded_text(self):
        with self.assertRaises(Exception):
            InboxPost(update_id="1", kind="message", channel_id=CHANNEL, message_id="6", posted_at=datetime(2025, 1, 1), text="x")
        with self.assertRaises(Exception):
            InboxPost(
                update_id="1", kind="channel_post", channel_id=CHANNEL, message_id="6",
                posted_at=datetime(2025, 1, 1), text="я" * 5000,
            )

    def test_committing_marks_every_pending_row_of_the_message(self):
        accept_post(self.conn, self.post("1", text="первая"))
        accept_post(self.conn, self.post("2", text="вторая"))
        pending = pending_posts(self.conn)
        self.assertEqual(1, len(pending))
        self.assertEqual("вторая", pending[0].text)
        marked = commit_posts(self.conn, pending, now=datetime(2025, 7, 31, tzinfo=timezone.utc))
        self.assertEqual(2, marked)
        self.assertEqual(0, inbox_counts(self.conn)["pending_messages"])
        self.assertEqual((), pending_posts(self.conn))

    def test_parsing_refuses_a_payload_that_is_not_a_channel_post(self):
        self.assertIsNone(parse_update({"update_id": 1}))
        with self.assertRaises(Exception):
            parse_update("not an object")

    def test_the_import_module_is_importable_through_the_package(self):
        from randreport import inbox

        self.assertIs(inbox.import_public_posts, import_json.import_public_posts)
        self.assertTrue(hasattr(inbox, "TelegramWebhook"))
        self.assertEqual("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", SECRET_HEADER)


if __name__ == "__main__":
    unittest.main()
