"""H01 — the historical plane: coverage, overlaps, ambiguity and prototype numbers.

docs/REQUIREMENTS.md sections 3.7, 12, 13. A historical aggregate is an
observation, not a transaction; only a *published, complete, whole-period* figure
can become a fact; the 2025 calendar stays partial; a missing value is not a
zero, and a prototype number never becomes verified.
"""

from __future__ import annotations

import shutil
import unittest
from datetime import date

from randreport import history
from randreport.admin import checks, desk, diagnostics, history_admin, members
from randreport.domain.timeframes import parse_period, range_period
from randreport.store import connect
from randreport.store import repository as repo
from tests import fixtures_admin as fa


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = fa.admin_case_dir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = connect(str(self.tmp / "randreport.sqlite"))
        self.addCleanup(self.conn.close)
        members.ensure_bootstrap(self.conn, fa.ADMIN_ID)


class CoverageTests(HistoryTestCase):
    def test_annual_is_not_the_sum_of_the_published_months(self) -> None:
        # the published year aggregate is stated as it was published
        self.assertEqual(history.metric_view(parse_period("2025"), "events").value, 459)
        # summing the monthly figures would give 298 (40+52+59+147), which is not the year
        monthly = sum(
            history.metric_view(parse_period(label), "events").value or 0
            for label in ("2025-01", "2025-02", "2025-03")
        )
        self.assertEqual(monthly, 151)
        self.assertNotEqual(monthly, 459)
        self.assertEqual(history.metric_view(range_period("2025-01", "2025-08"), "events").value, 298)

    def test_the_group_report_is_not_split_into_months(self) -> None:
        group = history.metric_view(range_period("2025-04", "2025-08"), "events")
        self.assertEqual(group.value, 147)
        self.assertEqual(group.coverage, history.COMPLETE)
        month = history.metric_view(parse_period("2025-07"), "events")
        self.assertIsNone(month.value)
        self.assertEqual(month.coverage, history.PARTIAL)
        self.assertIn("2025-04..2025-08", month.detail)
        self.assertIsNone(history.metric_view(range_period("2025-02", "2025-11"), "events").value)

    def test_the_missing_months_of_2025_are_named_not_invented(self) -> None:
        gaps = {gap["metric"]: gap for gap in history_admin.gaps(self.conn)}
        self.assertIn("events", gaps)
        self.assertEqual(gaps["events"]["missing"], ["2025-09", "2025-10", "2025-11", "2025-12"])
        self.assertEqual(gaps["events"]["covered"][0], "2025-01")
        for month in ("2025-09", "2025-10", "2025-11", "2025-12"):
            view = history.metric_view(parse_period(month), "events")
            self.assertIsNone(view.value)
            self.assertNotEqual(view.coverage, history.COMPLETE)
        findings = diagnostics.collect(self.conn)
        gap = next(item for item in findings if item.code == "historical_gap")
        self.assertIn("2025-09", gap.periods)
        self.assertIn("не дел", gap.significance)

    def test_missing_is_not_a_zero(self) -> None:
        with_zero = history_admin.Observation(
            observation_id="obs_synthetic_zero",
            metric="receipts",
            period_from="2025-06",
            period_to="2025-06",
            grain=history.MONTH,
            value=0,
            unit="RUB",
            source_id="tg:random_culture:999",
            provenance="published",
            coverage=history.COMPLETE,
            status=history_admin.OK,
            note="подтверждённое отсутствие поступлений",
        )
        unresolved = [item for item in history_admin.observations(self.conn) if item.value is None]
        self.assertTrue(unresolved, "the fixtures keep unconfirmed observations without a value")
        for item in unresolved:
            self.assertEqual(item.display_value, "нет данных")
        self.assertEqual(with_zero.display_value, "0")
        self.assertTrue(with_zero.promotable)
        self.assertFalse(unresolved[0].promotable)


class PromotableFactTests(HistoryTestCase):
    def test_only_published_complete_periods_become_facts(self) -> None:
        facts = history_admin.verified_facts(self.conn)
        self.assertTrue(facts)
        for fact in facts:
            self.assertEqual(fact.provenance, "published")
            self.assertEqual(fact.coverage, history.COMPLETE)
            self.assertIsNotNone(fact.value)
            self.assertIsNotNone(fact.period_from)
        values = {fact.metric: fact.value for fact in facts if fact.grain == history.YEAR}
        self.assertEqual(values["events"], 459)
        self.assertEqual(values["receipts"], 707920)
        self.assertEqual(values["expenses"], 1267226)

    def test_prototype_and_ambiguous_observations_cannot_be_promoted(self) -> None:
        candidates = history_admin.candidate_observations()
        self.assertTrue(candidates)
        for candidate in candidates:
            with self.subTest(candidate=candidate.observation_id):
                self.assertFalse(candidate.promotable)
                with self.assertRaises(history_admin.HistoryError) as caught:
                    history_admin.promote(self.conn, candidate.observation_id, actor=fa.ADMIN_ID)
                self.assertIn("нельзя превратить в факт", str(caught.exception))
        statuses = {candidate.status for candidate in candidates}
        self.assertIn(history_admin.AMBIGUOUS_YEAR, statuses)
        self.assertIn(history_admin.NEEDS_SOURCE, statuses)

    def test_an_unresolved_observation_is_not_counted_anywhere(self) -> None:
        unresolved = history_admin.unresolved(self.conn)
        self.assertTrue(all(item.value is None for item in unresolved))
        totals = sum(item.value or 0 for item in history_admin.verified_facts(self.conn) if item.metric == "expenses")
        self.assertEqual(totals, 1267226)
        overview = desk.overview(self.conn)
        self.assertEqual(overview["history"]["unresolved"], len(unresolved))
        findings = diagnostics.collect(self.conn)
        item = next(finding for finding in findings if finding.code == "historical_unresolved")
        self.assertEqual(item.payload["count"], len(unresolved))

    def test_a_published_observation_can_be_promoted_and_is_audited(self) -> None:
        fact = next(item for item in history_admin.verified_facts(self.conn) if item.grain == history.YEAR)
        promoted = history_admin.promote(self.conn, fact.observation_id, actor=fa.ADMIN_ID)
        self.assertEqual(promoted.observation_id, fact.observation_id)
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("history.promoted", actions)

    def test_ingest_is_idempotent(self) -> None:
        before = len(history_admin.observations(self.conn))
        history_admin.ingest(self.conn)
        self.assertEqual(len(history_admin.observations(self.conn)), before)


class ConflictTests(HistoryTestCase):
    def test_overlapping_variants_are_recorded_as_an_open_conflict(self) -> None:
        conflicts = history_admin.conflicts(self.conn)
        self.assertTrue(conflicts)
        self.assertTrue(all(item.status == "open" for item in conflicts))
        group = next(item for item in conflicts if item.conflict_group == "internal-book:variants")
        self.assertIn("перекрываются", group.notes or "")
        findings = diagnostics.collect(self.conn)
        conflict = next(item for item in findings if item.code == "historical_conflict")
        self.assertEqual(conflict.payload["group"], "internal-book:variants")

    def test_a_same_period_conflict_is_detected_from_published_values(self) -> None:
        conflict = history_admin.record_conflict(
            self.conn,
            conflict_id="conf_synthetic_same_period",
            conflict_group="events:2025-03",
            metric="events",
            period_from="2025-03",
            period_to="2025-03",
            published_value=59,
            computed_value=57,
            notes="два опубликованных значения за один период",
        )
        self.assertEqual(conflict.difference, "2")
        detected = [item for item in history_admin.conflicts(self.conn) if item.conflict_id == "conf_synthetic_same_period"]
        self.assertEqual(len(detected), 1)

    def test_verification_does_not_close_a_conflict_and_closing_it_does(self) -> None:
        diagnostics.sync_issues(self.conn, diagnostics.collect(self.conn))
        open_before = [item for item in diagnostics.issues(self.conn) if item["code"] == "historical_conflict"]
        self.assertEqual(len(open_before), 1)
        checks.verify(self.conn, entity_type="historical_conflict", entity_key=str(open_before[0]["entity_key"]), actor=fa.ADMIN_ID)
        still = [item for item in diagnostics.issues(self.conn, status=diagnostics.OPEN) if item["code"] == "historical_conflict"]
        self.assertEqual(len(still), 1, "a manual check must not close a historical conflict")

        conflict_id = str(open_before[0]["entity_key"])
        self.assertTrue(history_admin.close_conflict(self.conn, conflict_id, actor=fa.ADMIN_ID, note="вариант книги подтверждён владельцем"))
        diagnostics.sync_issues(self.conn, diagnostics.collect(self.conn))
        remaining = [item for item in diagnostics.issues(self.conn, status=diagnostics.OPEN) if item["code"] == "historical_conflict"]
        self.assertEqual(remaining, [])
        resolved = [item for item in diagnostics.issues(self.conn, status=diagnostics.RESOLVED) if item["code"] == "historical_conflict"]
        self.assertEqual(len(resolved), 1)

    def test_a_part_cannot_exceed_its_whole(self) -> None:
        high = history_admin.record_conflict(
            self.conn,
            conflict_id="conf_synthetic_containment",
            conflict_group="events:containment",
            metric="events",
            period_from="2025-02",
            period_to="2025-02",
            published_value=52,
            computed_value=520,
            notes="внутренняя книга утверждает больше, чем годовая публикация",
        )
        self.assertEqual(high.difference, "-468")
        self.assertEqual(history_admin.conflicts(self.conn, status="open")[-1].status, "open")


class DeskHistoryTests(HistoryTestCase):
    def test_the_desk_states_the_partial_year_and_the_gap(self) -> None:
        diagnostics.sync_issues(self.conn, diagnostics.collect(self.conn))
        overview = desk.overview(self.conn)
        self.assertIn("2025", overview["history"]["gaps"])
        self.assertGreaterEqual(overview["history"]["conflicts_open"], 1)
        self.assertGreaterEqual(overview["history"]["unresolved"], 1)
        problems = {item["code"] for item in overview["problems"]}
        self.assertTrue(problems & {"historical_gap", "historical_conflict", "historical_unresolved"})

    def test_history_facts_are_dated_and_carry_their_source(self) -> None:
        facts = history_admin.verified_facts(self.conn)
        for fact in facts:
            self.assertIn("tg:random_culture:", fact.source_id)
            self.assertRegex(fact.period_from or "", r"^\d{4}-\d{2}$")

    def test_the_year_view_of_the_desk_does_not_invent_a_balance(self) -> None:
        annual = history.annual_view(2025)
        receipts, expenses = annual[1], annual[2]
        self.assertEqual(receipts.value, 707920)
        self.assertEqual(expenses.value, 1267226)
        self.assertIsNone(history.coverage_percent(None, expenses.value))
        self.assertEqual(history.coverage_percent(receipts.value, expenses.value), __import__("decimal").Decimal("55.9"))
        self.assertIsNone(repo.latest_report_snapshot(self.conn, "2025"))

    def test_an_observation_without_a_confirmed_year_stays_out_of_the_totals(self) -> None:
        note = next(item for item in history_admin.unresolved(self.conn) if item.status == history_admin.AMBIGUOUS_YEAR)
        self.assertIsNone(note.period_from)
        self.assertIn("не подтверждён", note.period_label)
        self.assertEqual(note.display_value, "нет данных")
        # nothing in the diagnostics or the desk turns it into a fact
        self.assertNotIn(note.observation_id, [fact.observation_id for fact in history_admin.verified_facts(self.conn)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
