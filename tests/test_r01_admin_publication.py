"""R01 — the version archive, its immutability and the publication desk.

The public side of R01 (permanent URLs, the correction diff on the page) is
covered by ``tests/test_web_public_site.py``. Here the *editorial* side is
tested: closing a period freezes one immutable version, corrections arrive as new
versions with a diff and a reason, the draft post never mixes the historical and
the live plane, and the generated text states no number the version lacks.
"""

from __future__ import annotations

import json
import shutil
import unittest

from randreport.admin import checks, diagnostics, members, reports
from randreport.store import connect
from randreport.store import repository as repo
from randreport.web.app import PublicSite
from randreport.web import reports as public_reports
from tests import fixtures_admin as fa
from tests.fixtures_admin import WsgiClient


class PublicationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = fa.admin_case_dir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = connect(str(self.tmp / "randreport.sqlite"))
        self.addCleanup(self.conn.close)
        self.releases = self.tmp / "releases"
        members.ensure_bootstrap(self.conn, fa.ADMIN_ID)

    def public_site(self) -> PublicSite:
        site = PublicSite(release_dir=self.releases, db_path=self.tmp / "randreport.sqlite")
        self.addCleanup(site.close)
        return site


class ClosingAndCorrectionTests(PublicationTestCase):
    def test_closing_freezes_version_one_from_the_live_plane(self) -> None:
        result = reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="месяц закрыт")
        self.assertEqual(result["version"], 1)
        stored = repo.latest_report_snapshot(self.conn, "2025-07")
        payload = json.loads(stored["payload"])
        self.assertEqual(payload["provenance"], public_reports.RECOMPUTED)
        self.assertEqual(payload["plane"], reports.LIVE)
        self.assertEqual(payload["frozen_by"], fa.ADMIN_ID)
        state = reports.publication_state(self.conn, "2025-07")
        self.assertEqual(state["status"], reports.CLOSED)
        self.assertEqual(state["frozen_version"], 1)
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("report.closed", actions)

    def test_a_frozen_version_is_never_rewritten(self) -> None:
        reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="закрытие")
        frozen = repo.latest_report_snapshot(self.conn, "2025-07")
        with self.assertRaises(ValueError):
            repo.record_report_snapshot(
                self.conn,
                report_id="report:2025-07:v1",
                period_label="2025-07",
                version=1,
                published_at="2026-01-01T00:00:00+00:00",
                payload={"period": "2025-07", "receipts": 1, "expenses": 1, "events": 1},
            )
        again = repo.latest_report_snapshot(self.conn, "2025-07")
        self.assertEqual(again["payload"], frozen["payload"])
        # an identical re-record is a no-op, not an error
        self.assertEqual(
            repo.record_report_snapshot(
                self.conn,
                report_id="report:2025-07:v1",
                period_label="2025-07",
                version=1,
                published_at=frozen["published_at"],
                payload=json.loads(frozen["payload"]),
                original_text=frozen["original_text"],
                original_url=frozen["original_url"],
            ),
            "unchanged",
        )

    def test_correction_adds_a_version_with_a_diff_and_keeps_the_original(self) -> None:
        reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="закрытие")
        original = repo.latest_report_snapshot(self.conn, "2025-07")
        result = reports.correction(
            self.conn,
            label="2025-07",
            changes={"events": 7},
            reason="нашли седьмое мероприятие в архиве календаря",
            actor=fa.ADMIN_ID,
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["diff"]["events"], {"from": 6, "to": 7})
        versions = reports.versions(self.conn, "2025-07")
        self.assertEqual([int(item["version"]) for item in versions], [1, 2])
        self.assertEqual(repo.latest_report_snapshot(self.conn, "2025-07")["payload"], json.dumps(result["payload"], ensure_ascii=False, sort_keys=True))
        self.assertEqual(original["payload"], json.dumps(json.loads(original["payload"]), ensure_ascii=False, sort_keys=True))
        corrections = json.loads(versions[1]["payload"])["corrections"]
        self.assertEqual(corrections[0]["reason"], "нашли седьмое мероприятие в архиве календаря")
        self.assertEqual(corrections[0]["actor"], fa.ADMIN_ID)

    def test_a_correction_needs_a_reason_and_an_allowed_field(self) -> None:
        reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="закрытие")
        with self.assertRaises(public_reports.ReportError):
            reports.correction(self.conn, label="2025-07", changes={"events": 5}, reason="", actor=fa.ADMIN_ID)
        with self.assertRaises(public_reports.ReportError):
            reports.correction(self.conn, label="2025-07", changes={"balance": 1}, reason="нельзя", actor=fa.ADMIN_ID)
        with self.assertRaises(public_reports.ReportError):
            reports.correction(self.conn, label="2025-07", changes={"events": 6}, reason="без изменений", actor=fa.ADMIN_ID)

    def test_correction_invalidates_the_period_check_but_keeps_the_text_faithful(self) -> None:
        reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="закрытие")
        checks.verify(self.conn, entity_type="report_period", entity_key="2025-07", actor=fa.ADMIN_ID)
        reports.correction(self.conn, label="2025-07", changes={"expenses": 5_500_000}, reason="уточнили расходы", actor=fa.ADMIN_ID)
        self.assertEqual(checks.effective_state(self.conn, "report_period", "2025-07"), checks.OPERATIONAL)
        for version in (1, 2):
            text = reports.text_for_version(self.conn, "2025-07", version)
            payload = public_reports.version_payload(self.conn, "2025-07", version)
            public_reports.assert_text_faithful(text, payload)

    def test_closing_without_data_or_reason_is_refused(self) -> None:
        with self.assertRaises(reports.PublicationError):
            reports.close_period(self.conn, label="2030-01", actor=fa.ADMIN_ID, reason="нечего закрывать")
        with self.assertRaises(reports.PublicationError):
            reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="")

    def test_unknown_version_has_no_text(self) -> None:
        self.assertIsNone(reports.text_for_version(self.conn, "2025-07", 99))
        with self.assertRaises(reports.PublicationError):
            reports.export_markdown(self.conn, label="2025-07", version=99)


class PlaneSeparationTests(PublicationTestCase):
    def _store_live_release_for(self, label: str) -> str:
        current = repo.current_release(self.conn)
        payload = json.loads(current["payload"])
        payload["money"]["period"]["requested"] = label
        payload["money"]["metrics"]["compensation_kopecks"] = 999_999_999
        payload["money"]["metrics"]["expense_kopecks"] = 111_111_111
        payload["events"]["metrics"]["total_events"] = 42
        repo.store_release(
            self.conn,
            release_id="rel_synthetic_overlap",
            as_of=current["as_of"],
            schema_version="public-release-1",
            sections=json.loads(current["sections"]),
            payload_text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        return "rel_synthetic_overlap"

    def test_period_with_both_planes_uses_one_and_never_adds_them(self) -> None:
        self._store_live_release_for("2025-01")
        plane = reports.plane_for(self.conn, "2025-01")
        self.assertEqual(plane.kind, reports.HISTORICAL)
        payload = reports.draft_payload(self.conn, "2025-01")
        self.assertEqual(payload["plane"], reports.HISTORICAL)
        self.assertEqual(payload["events"], 40)
        self.assertIsNone(payload["receipts"])
        self.assertNotEqual(payload["receipts"], 999_999_999)
        self.assertNotEqual(payload["expenses"], 111_111_111)
        self.assertNotEqual(payload["events"], 40 + 42)
        planes = reports.planes(self.conn, None, "2025-01")
        self.assertTrue(planes["overlap"])
        self.assertIn("не складывает", planes["note"])
        text = reports.draft_text(payload)
        self.assertIn("нет данных", text)
        self.assertNotIn("999", text)

    def test_period_without_a_published_report_uses_the_live_plane(self) -> None:
        plane = reports.plane_for(self.conn, "2025-07")
        self.assertEqual(plane.kind, reports.LIVE)
        self.assertEqual(reports.planes(self.conn, None, "2025-07")["overlap"], False)

    def test_draft_text_states_only_numbers_the_version_contains(self) -> None:
        built = reports.build(self.conn, label="2025-07")
        public_reports.assert_text_faithful(built["text"], built["payload"])
        doctored = built["text"] + "\nИтого: 123456789 ₽"
        with self.assertRaises(public_reports.ReportError):
            public_reports.assert_text_faithful(doctored, built["payload"])

    def test_an_unpublished_period_states_no_data(self) -> None:
        built = reports.build(self.conn, label="2024-01")
        self.assertEqual(built["plane"], reports.NONE)
        self.assertIn("нет данных", built["text"])
        self.assertFalse(built["readiness"]["ready"])
        self.assertIn("нет данных ни из одной плоскости", built["readiness"]["blockers"][0])


class DraftReadinessTests(PublicationTestCase):
    def test_readiness_blocks_until_the_period_is_clean(self) -> None:
        diagnostics.sync_issues(self.conn, diagnostics.collect(self.conn))
        checks.verify(self.conn, entity_type="period", entity_key="2025-07", actor=fa.ADMIN_ID)
        report = reports.readiness(self.conn, "2025-07")
        self.assertFalse(report["ready"])
        self.assertTrue(any("проблема" in item for item in report["blockers"]))
        with self.assertRaises(reports.PublicationError):
            reports.save_draft(self.conn, label="2025-07", actor=fa.ADMIN_ID, status=reports.READY)

        diagnostics.sync_issues(self.conn, [])  # every catalogued condition is gone
        diagnostics.sync_issues(self.conn, diagnostics.collect(self.conn))
        checks.verify(self.conn, entity_type="period", entity_key="2025-07", actor=fa.ADMIN_ID)
        report = reports.readiness(self.conn, "2025-07")
        self.assertTrue(report["ready"], report["blockers"])
        saved = reports.save_draft(self.conn, label="2025-07", actor=fa.ADMIN_ID, status=reports.READY, comment="публикуем")
        self.assertEqual(saved["status"], reports.READY)
        self.assertEqual(saved["comment"], "публикуем")

    def test_an_unknown_status_is_refused(self) -> None:
        with self.assertRaises(reports.PublicationError):
            reports.save_draft(self.conn, label="2025-07", actor=fa.ADMIN_ID, status="published-live")

    def test_markdown_export_carries_no_private_identifiers(self) -> None:
        reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="закрытие")
        markdown = reports.exported_periods(self.conn, periods=["2025-07"])["2025-07"]
        for token in ("tx_", "acc_", "cat_", "occ_", "pair_", "account_id", "<"):
            self.assertNotIn(token, markdown)
        self.assertIn("## Отчёт за 2025-07", markdown)
        self.assertIn("Поступления", markdown)


class PublicWiringTests(PublicationTestCase):
    def test_the_public_page_serves_the_frozen_versions_the_desk_produced(self) -> None:
        reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="закрытие")
        reports.correction(self.conn, label="2025-07", changes={"events": 8}, reason="поздний отчёт", actor=fa.ADMIN_ID)
        site = self.public_site()
        page = WsgiClient(site, host="public.example").request("GET", "/reports/2025-07")
        self.assertEqual(page.status, 200)
        self.assertIn("v1", page.text)
        self.assertIn("v2", page.text)
        self.assertIn("поздний отчёт", page.text)
        self.assertIn("/reports/2025-07/v1", page.text)
        self.assertNotIn("tx_", page.text)
        version_one = WsgiClient(site, host="public.example").request("GET", "/reports/2025-07/v1")
        self.assertEqual(version_one.status, 200)
        # v1 still states the original figure, v2 the corrected one
        self.assertIn("6", version_one.text)

    def test_closing_does_not_block_the_live_release(self) -> None:
        """The site stays public after the nightly sync; closing is not a gate."""

        site = self.public_site()
        before = WsgiClient(site, host="public.example").request("GET", "/api/public/money?period=2025-07")
        self.assertEqual(before.status, 200)
        reports.close_period(self.conn, label="2025-07", actor=fa.ADMIN_ID, reason="закрытие")
        after = WsgiClient(site, host="public.example").request("GET", "/api/public/money?period=2025-07")
        self.assertEqual(after.status, 200)
        self.assertEqual(before.body, after.body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
