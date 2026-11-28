"""A02 — equal access, revision-bound verification, invalidation, open discrepancies.

docs/REQUIREMENTS.md sections 4, 11, 13: every active commune member can verify;
a check belongs to an entity *and* a revision, so a source, mapping, privacy or
methodology change resets only the affected checks; a manual "verified" never
closes a real discrepancy.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from randreport.admin import checks, desk, diagnostics, members, news
from randreport.store import connect
from tests import fixtures_admin as fa


class DeskTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = fa.admin_case_dir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = connect(str(self.tmp / "randreport.sqlite"))
        self.addCleanup(self.conn.close)
        members.ensure_bootstrap(self.conn, fa.ADMIN_ID)
        members.add_member(self.conn, fa.SECOND_ADMIN_ID)

    def _transaction(self) -> str:
        return str(self.conn.execute("SELECT transaction_id FROM finance_transactions WHERE deleted = 0 ORDER BY transaction_id").fetchone()["transaction_id"])

    def _news(self) -> str:
        # the chronicle item; the pipeline also stores announcement rows now
        return str(self.conn.execute("SELECT news_id FROM news_items WHERE classification = 'news' LIMIT 1").fetchone()["news_id"])


class EqualAccessTests(DeskTestCase):
    def test_every_active_member_can_verify_and_change_mapping(self) -> None:
        entity = self._transaction()
        first = checks.verify(self.conn, entity_type="finance_transaction", entity_key=entity, actor=fa.ADMIN_ID)
        self.assertEqual(first.state, checks.VERIFIED)
        checks.invalidate(self.conn, entity_type="finance_transaction", entity_key=entity, cause="test")
        second = checks.verify(self.conn, entity_type="finance_transaction", entity_key=entity, actor=fa.SECOND_ADMIN_ID)
        self.assertEqual(second.state, checks.VERIFIED)
        version = desk.set_category_mapping(self.conn, category_id="cat_rent", label="Аренда зала", actor=fa.SECOND_ADMIN_ID)
        self.assertTrue(version)

    def test_all_members_are_listed_with_equal_standing(self) -> None:
        view = desk.settings_view(self.conn)
        ids = [item["telegram_user_id"] for item in view["members"]]
        self.assertEqual(sorted(ids), sorted([fa.ADMIN_ID, fa.SECOND_ADMIN_ID]))
        self.assertTrue(all(item["active"] for item in view["members"]))
        self.assertNotIn("role", json.dumps(view))

    def test_every_action_is_attributed_in_the_internal_audit(self) -> None:
        checks.verify(self.conn, entity_type="period", entity_key="2025-07", actor=fa.SECOND_ADMIN_ID)
        actor = self.conn.execute("SELECT actor FROM audit_log WHERE action LIKE 'check.%' ORDER BY audit_id DESC LIMIT 1").fetchone()["actor"]
        self.assertEqual(actor, fa.SECOND_ADMIN_ID)
        trail = desk.audit_trail(self.conn)
        self.assertTrue(any(item["actor"] == fa.SECOND_ADMIN_ID for item in trail))


class RevisionBoundCheckTests(DeskTestCase):
    def test_verification_is_bound_to_the_revision(self) -> None:
        entity = self._transaction()
        checks.verify(self.conn, entity_type="finance_transaction", entity_key=entity, actor=fa.ADMIN_ID)
        self.assertEqual(checks.effective_state(self.conn, "finance_transaction", entity), checks.VERIFIED)
        # an annotation is not a significant field: the verification survives
        desk.annotate(self.conn, transaction_id=entity, note="уточнение", actor=fa.ADMIN_ID)
        self.assertEqual(checks.effective_state(self.conn, "finance_transaction", entity), checks.VERIFIED)
        # the source changes the row: the verification no longer applies
        self.conn.execute("UPDATE finance_transactions SET amount_kopecks = amount_kopecks + 1 WHERE transaction_id = ?", (entity,))
        self.assertEqual(checks.effective_state(self.conn, "finance_transaction", entity), checks.STALE)

    def test_mapping_change_invalidates_finance_and_period_but_not_the_channel(self) -> None:
        day = str(self.conn.execute("SELECT on_date FROM channel_snapshots LIMIT 1").fetchone()["on_date"])
        entity = self._transaction()
        checks.verify(self.conn, entity_type="finance_transaction", entity_key=entity, actor=fa.ADMIN_ID)
        checks.verify(self.conn, entity_type="channel_snapshot", entity_key=day, actor=fa.ADMIN_ID)
        checks.verify(self.conn, entity_type="period", entity_key="2025-07", actor=fa.ADMIN_ID)

        version = desk.set_category_mapping(self.conn, category_id="cat_rent", economic_class="operating_expense", actor=fa.ADMIN_ID)
        self.assertTrue(version)
        self.assertEqual(checks.effective_state(self.conn, "finance_transaction", entity), checks.OPERATIONAL)
        self.assertEqual(checks.effective_state(self.conn, "period", "2025-07"), checks.OPERATIONAL)
        self.assertEqual(checks.effective_state(self.conn, "channel_snapshot", day), checks.VERIFIED)

        timed = checks.invalidations(self.conn)
        causes = {item["cause"] for item in timed}
        self.assertIn("mapping", causes)
        self.assertTrue(all(item["rules_version"] == checks.RULES_VERSION for item in timed))

    def test_event_mapping_change_touches_events_only(self) -> None:
        key = str(self.conn.execute("SELECT calendar_id, occurrence_id FROM calendar_occurrences LIMIT 1").fetchone()["occurrence_id"])
        occurrence = str(self.conn.execute("SELECT calendar_id || '|' || occurrence_id AS key FROM calendar_occurrences LIMIT 1").fetchone()["key"])
        entity = self._transaction()
        checks.verify(self.conn, entity_type="calendar_occurrence", entity_key=occurrence, actor=fa.ADMIN_ID)
        checks.verify(self.conn, entity_type="finance_transaction", entity_key=entity, actor=fa.ADMIN_ID)
        desk.set_event_mapping(self.conn, subcalendar_id="sub_closed", event_type="closed", private=True, actor=fa.ADMIN_ID)
        self.assertEqual(checks.effective_state(self.conn, "calendar_occurrence", occurrence), checks.OPERATIONAL)
        self.assertEqual(checks.effective_state(self.conn, "finance_transaction", entity), checks.VERIFIED)

    def test_news_edit_invalidates_only_that_item(self) -> None:
        news_id = self._news()
        checks.verify(self.conn, entity_type="news_item", entity_key=news_id, actor=fa.ADMIN_ID)
        other = checks.verify(self.conn, entity_type="period", entity_key="2025-07", actor=fa.ADMIN_ID)
        news.edit(self.conn, news_id=news_id, actor=fa.ADMIN_ID, title="Новый заголовок")
        self.assertEqual(checks.effective_state(self.conn, "news_item", news_id), checks.OPERATIONAL)
        self.assertEqual(checks.effective_state(self.conn, "period", "2025-07"), other.state)

    def test_methodology_change_resets_period_checks_only(self) -> None:
        entity = self._transaction()
        checks.verify(self.conn, entity_type="finance_transaction", entity_key=entity, actor=fa.ADMIN_ID)
        checks.verify(self.conn, entity_type="period", entity_key="2025-07", actor=fa.ADMIN_ID)
        checks.invalidate_for_methodology(self.conn, version="methodology-2", actor=fa.ADMIN_ID)
        self.assertEqual(checks.effective_state(self.conn, "finance_transaction", entity), checks.VERIFIED)
        self.assertEqual(checks.effective_state(self.conn, "period", "2025-07"), checks.OPERATIONAL)

    def test_missing_entity_is_no_data(self) -> None:
        self.assertEqual(checks.effective_state(self.conn, "finance_transaction", "tx_not_here"), checks.NO_DATA)
        result = checks.verify(self.conn, entity_type="finance_transaction", entity_key="tx_not_here", actor=fa.ADMIN_ID)
        self.assertEqual(result.state, checks.NO_DATA)

    def test_summary_counts_only_effective_states(self) -> None:
        entity = self._transaction()
        checks.verify(self.conn, entity_type="finance_transaction", entity_key=entity, actor=fa.ADMIN_ID)
        self.conn.execute("UPDATE finance_transactions SET source_revision = source_revision + 1 WHERE transaction_id = ?", (entity,))
        summary = checks.summary(self.conn)
        self.assertEqual(summary[checks.STALE], 1)
        self.assertEqual(summary[checks.VERIFIED], 0)


class DiscrepancyTests(DeskTestCase):
    def test_manual_verification_does_not_close_a_discrepancy(self) -> None:
        findings = diagnostics.collect(self.conn)
        counts = diagnostics.sync_issues(self.conn, findings)
        self.assertGreater(counts["open"], 0)
        issue = next(item for item in diagnostics.issues(self.conn) if item["code"] == "category_unmapped")
        self.assertEqual(issue["status"], diagnostics.OPEN)

        result = checks.verify(self.conn, entity_type="finance_category", entity_key=str(issue["entity_key"]), actor=fa.ADMIN_ID)
        self.assertEqual(result.state, checks.CONFLICT)
        self.assertIn("расхождение остаётся открытым", result.note)
        self.assertEqual(diagnostics.issues(self.conn, status=diagnostics.OPEN)[0]["status"], diagnostics.OPEN)

        self.assertTrue(diagnostics.acknowledge(self.conn, str(issue["issue_id"]), actor=fa.ADMIN_ID, comment="знаем, классифицируем"))
        again = [item for item in diagnostics.issues(self.conn) if item["issue_id"] == issue["issue_id"]][0]
        self.assertEqual(again["status"], diagnostics.ACKNOWLEDGED)
        self.assertEqual(diagnostics.comment_of(again), "знаем, классифицируем")

    def test_an_issue_closes_only_when_its_condition_disappears(self) -> None:
        findings = diagnostics.collect(self.conn)
        diagnostics.sync_issues(self.conn, findings)
        before = len([item for item in diagnostics.issues(self.conn) if item["code"] == "category_unmapped"])
        self.assertEqual(before, 1)
        # map the category from the desk: the condition is gone, so the issue resolves
        desk.set_category_mapping(self.conn, category_id="cat_brand_new", economic_class="unclassified", label="Новая категория", actor=fa.ADMIN_ID)
        remaining = [finding for finding in diagnostics.collect(self.conn) if finding.code == "category_unmapped"]
        self.assertEqual(remaining, [])
        counts = diagnostics.sync_issues(self.conn, [finding for finding in diagnostics.collect(self.conn)])
        self.assertGreaterEqual(counts["resolved"], 1)
        resolved = diagnostics.issues(self.conn, status=diagnostics.RESOLVED)
        self.assertTrue(any(item["code"] == "category_unmapped" for item in resolved))
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("issue.resolved", actions)

    def test_findings_name_sources_periods_and_significance(self) -> None:
        findings = diagnostics.collect(self.conn)
        codes = {finding.code for finding in findings}
        self.assertIn("category_unmapped", codes)
        self.assertIn("historical_conflict", codes)
        self.assertIn("historical_gap", codes)
        for finding in findings:
            self.assertTrue(finding.explanation)
            self.assertTrue(finding.significance)
            self.assertIn(finding.severity, diagnostics.SEVERITIES)

    def test_diagnostics_never_expose_a_private_operation(self) -> None:
        payload = json.dumps([finding.payload for finding in diagnostics.collect(self.conn)], ensure_ascii=False)
        self.assertNotIn("tx_private", payload)
        self.assertNotIn("tx_mixed", payload)
        self.assertNotIn("acc_personal", payload)


class NewsModerationTests(DeskTestCase):
    def test_edit_keeps_the_machine_version_and_the_manual_one(self) -> None:
        news_id = self._news()
        before = news.public_items(self.conn)[0]
        result = news.edit(self.conn, news_id=news_id, actor=fa.ADMIN_ID, title="Отремонтировали вход", summary="Новые двери.", note="уточнили формулировку")
        versions = news.revisions(self.conn, news_id)
        self.assertEqual([item.origin for item in versions], ["llm", "manual"])
        self.assertEqual(result["manual_state"], news.EDITED)
        after = news.public_items(self.conn)[0]
        self.assertEqual(after["title"], "Отремонтировали вход")
        self.assertEqual(before["published_on"], after["published_on"])

    def test_hide_and_restore_are_audited_and_reversible(self) -> None:
        news_id = self._news()
        news.hide(self.conn, news_id=news_id, actor=fa.ADMIN_ID, reason="анонс, не новость")
        self.assertEqual(news.public_items(self.conn), ())
        row = self.conn.execute("SELECT manual_state FROM news_items WHERE news_id = ?", (news_id,)).fetchone()
        self.assertEqual(row["manual_state"], news.HIDDEN)
        news.restore(self.conn, news_id=news_id, actor=fa.ADMIN_ID)
        self.assertEqual(len(news.public_items(self.conn)), 1)
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("news.hidden", actions)
        self.assertIn("news.restored", actions)

    def test_merge_folds_source_messages_and_hides_the_absorbed_item(self) -> None:
        items = self.conn.execute("SELECT * FROM news_items WHERE classification = 'news'").fetchall()
        self.assertEqual(len(items), 1)
        row = items[0]
        from datetime import date

        from randreport.news.models import NewsItem
        from randreport.store import repository as repo

        duplicate = NewsItem(
            news_id="news_synthetic_duplicate",
            classification="news",
            category="ремонт",
            title="Вход в подвал отремонтирован",
            summary="Другая формулировка того же события.",
            published_on=date(2025, 7, 13),
            source_urls=(),
            source_message_ids=("700",),
            content_hash="synthetic",
        )
        repo.upsert_news_item(self.conn, duplicate)
        self.assertEqual(len(news.public_items(self.conn)), 2)
        result = news.merge(self.conn, target_id=str(row["news_id"]), source_id="news_synthetic_duplicate", actor=fa.ADMIN_ID, reason="одно событие")
        self.assertEqual(result["source_message_ids"], ["700"])
        self.assertEqual(len(news.public_items(self.conn)), 1)
        self.assertEqual(news.absorbed_message_ids(self.conn)["700"], str(row["news_id"]))

    def test_a_later_run_does_not_undo_a_manual_decision(self) -> None:
        news_id = self._news()
        news.edit(self.conn, news_id=news_id, actor=fa.ADMIN_ID, title="Правленый заголовок")
        row = self.conn.execute("SELECT * FROM news_items WHERE news_id = ?", (news_id,)).fetchone()

        from randreport.news.models import NewsItem
        from randreport.store import repository as repo

        again = NewsItem(
            news_id=news_id,
            classification=row["classification"],
            category=row["category"],
            title=row["title"],
            summary=row["summary"],
            published_on=__import__("datetime").date.fromisoformat(row["published_on"]),
            source_urls=(),
            source_message_ids=tuple(json.loads(row["source_message_ids"])),
            content_hash=str(row["content_hash"]),
        )
        outcome = repo.upsert_news_item(self.conn, again)
        self.assertEqual(outcome, "preserved")
        after = self.conn.execute("SELECT manual_state FROM news_items WHERE news_id = ?", (news_id,)).fetchone()
        self.assertEqual(after["manual_state"], news.EDITED)

    def test_re_extraction_duplicate_is_deduplicated_after_a_merge(self) -> None:
        items = self.conn.execute("SELECT * FROM news_items WHERE classification = 'news'").fetchall()
        row = items[0]
        from randreport.news.models import NewsItem
        from randreport.store import repository as repo
        from datetime import date

        duplicate = NewsItem(
            news_id="news_re_extracted",
            classification="news",
            category="ремонт",
            title="Тот же пост, другая модель",
            summary="Повторное извлечение.",
            published_on=date(2025, 7, 12),
            source_urls=(),
            source_message_ids=(str(json.loads(row["source_message_ids"])[0]),),
            content_hash="other",
        )
        repo.upsert_news_item(self.conn, duplicate)
        news.merge(self.conn, target_id=str(row["news_id"]), source_id="news_re_extracted", actor=fa.ADMIN_ID, reason="одно событие")
        # a brand-new id from the same messages must not reach the chronicle
        third = NewsItem(
            news_id="news_re_extracted_2",
            classification="news",
            category="ремонт",
            title="Ещё одна формулировка",
            summary="Третья попытка.",
            published_on=date(2025, 7, 12),
            source_urls=(),
            source_message_ids=(str(json.loads(row["source_message_ids"])[0]),),
            content_hash="third",
        )
        repo.upsert_news_item(self.conn, third)
        hidden = news.reconcile_merges(self.conn)
        self.assertEqual(hidden, 1)
        self.assertEqual(len(news.public_items(self.conn)), 1)
        self.assertEqual(news.public_items(self.conn)[0]["news_id"], str(row["news_id"]))

    def test_empty_or_overlong_edits_are_refused(self) -> None:
        news_id = self._news()
        with self.assertRaises(news.NewsError):
            news.edit(self.conn, news_id=news_id, actor=fa.ADMIN_ID)
        with self.assertRaises(news.NewsError):
            news.edit(self.conn, news_id=news_id, actor=fa.ADMIN_ID, title="   ")
        with self.assertRaises(news.NewsError):
            news.edit(self.conn, news_id=news_id, actor=fa.ADMIN_ID, title="x" * (news.MAX_TITLE + 1))
        with self.assertRaises(news.NewsError):
            news.hide(self.conn, news_id=news_id, actor=fa.ADMIN_ID, reason="")
        with self.assertRaises(news.NewsError):
            news.merge(self.conn, target_id=news_id, source_id=news_id, actor=fa.ADMIN_ID, reason="сама с собой")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
