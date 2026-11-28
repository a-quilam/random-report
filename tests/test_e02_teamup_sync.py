"""E02 — Teamup occurrences through the connector and the store.

docs/REQUIREMENTS.md section 7 and the E02 row of the acceptance matrix:
recurrence, several calendars, moves, explicit deletion, retention and unknown
types, exercised on the real read-only client plus the domain rules.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.events import event_stats, select_completed  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402

PAGE_ONE = {"events": [], "pages": {"next": None}}


def page(events):
    return {"events": list(events), "pages": {"next": None}}


class TeamupSyncCase(unittest.TestCase):
    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")

    def tearDown(self):
        self.tmp.cleanup()
        self.conn.close()

    def run_teamup(self, pages, *, now=ig.NOW):
        client, transport = ig.teamup(pages=pages)
        report = ig.run_once(self.conn, [ig.teamup_adapter(client)], now=now, lease=self.lease)
        return report, client, transport

    def events_payload(self):
        return json.loads(repo.current_release(self.conn)["payload"])["events"]


class TestE02Occurrences(TeamupSyncCase):
    def test_recurring_occurrences_are_counted_per_instance(self):
        events = [
            ig.teamup_event(f"occ_{i}", f"2025-07-0{i}T18:00:00+03:00", f"2025-07-0{i}T20:00:00+03:00", series_id="ser_1")
            for i in (1, 2, 3)
        ]
        report, _, _ = self.run_teamup([page(events)])
        self.assertEqual("ok", report.sources["teamup"].status)
        self.assertEqual(3, len(repo.load_occurrences(self.conn)))
        self.assertEqual(3, self.events_payload()["metrics"]["total_events"])

    def test_a_record_listed_in_several_subcalendars_counts_once(self):
        events = [
            ig.teamup_event(
                "occ_multi",
                "2025-07-03T18:00:00+03:00",
                "2025-07-03T20:00:00+03:00",
                subcalendars=("sub_open", "sub_preparation"),
            )
        ]
        self.run_teamup([page(events)])
        occurrences = repo.load_occurrences(self.conn)
        self.assertEqual(1, len(occurrences))
        selection = select_completed(occurrences, mapping=fx.event_mapping(), cutoff=ig.NOW)
        self.assertEqual(1, event_stats(selection, cutoff=ig.NOW).total)
        self.assertEqual(1, self.events_payload()["event_type_aggregates"]["preparation"])

    def test_a_move_updates_the_row_instead_of_duplicating_it(self):
        client, transport = ig.teamup(pages=[page([ig.teamup_event("occ_move", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00")])])
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        transport.handler = ig.teamup_pages(
            [page([ig.teamup_event("occ_move", "2025-07-04T19:00:00+03:00", "2025-07-04T21:00:00+03:00", version=2)])]
        )
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        rows = self.conn.execute("SELECT start_utc, revision FROM calendar_occurrences").fetchall()
        self.assertEqual(1, len(rows))
        self.assertIn("2025-07-04", rows[0]["start_utc"])

    def test_an_explicit_deletion_leaves_the_operational_stats_but_keeps_the_row(self):
        client, transport = ig.teamup(
            pages=[page([ig.teamup_event("occ_gone", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00")])]
        )
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        transport.handler = ig.teamup_pages([page([ig.teamup_event("occ_gone", "2025-07-03T18:00:00+03:00", None, delete=True)])])
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)

        occurrences = repo.load_occurrences(self.conn)
        self.assertEqual(1, len(occurrences))
        self.assertTrue(occurrences[0].deleted)
        selection = select_completed(occurrences, mapping=fx.event_mapping(), cutoff=ig.NOW)
        self.assertEqual(0, event_stats(selection, cutoff=ig.NOW).total)
        self.assertEqual(["occ_gone"], [o.occurrence_id for o in selection.deleted])

    def test_retention_on_the_source_does_not_erase_the_local_archive(self):
        client, transport = ig.teamup(
            pages=[page([ig.teamup_event("occ_archived", "2025-07-06T18:00:00+03:00", "2025-07-06T20:00:00+03:00")])]
        )
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        transport.handler = ig.teamup_pages([page([])])
        report = ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)

        self.assertEqual(1, report.sources["teamup"].payload["archived_by_retention"])
        occurrences = repo.load_occurrences(self.conn)
        self.assertEqual(1, len(occurrences))
        self.assertTrue(occurrences[0].local_archive)
        self.assertFalse(occurrences[0].source_present)
        self.assertEqual(1, self.events_payload()["metrics"]["local_archive_events"])

    def test_an_unknown_type_is_counted_without_public_details(self):
        client, _ = ig.teamup(
            pages=[
                page(
                    [
                        ig.teamup_event(
                            "occ_unknown",
                            "2025-07-07T19:00:00+03:00",
                            "2025-07-07T21:00:00+03:00",
                            subcalendars=("sub_lecture",),
                            title="СЕКРЕТНАЯ ЛЕКЦИЯ АРЕНДАТОРА",
                        )
                    ]
                )
            ]
        )
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        payload = self.events_payload()
        self.assertEqual(1, payload["event_type_aggregates"]["unknown"])
        self.assertEqual([], payload["public_events"])
        self.assertEqual({}, payload["calendar_days"])
        self.assertNotIn("СЕКРЕТНАЯ ЛЕКЦИЯ АРЕНДАТОРА", repo.current_release(self.conn)["payload"])

    def test_pagination_follows_every_page(self):
        client, transport = ig.teamup(
            pages=[
                {"events": [ig.teamup_event("occ_p1", "2025-07-01T18:00:00+03:00", "2025-07-01T20:00:00+03:00")], "pages": {"next": {"page": 2}}},
                {"events": [ig.teamup_event("occ_p2", "2025-07-02T18:00:00+03:00", "2025-07-02T20:00:00+03:00")], "pages": {"next": None}},
            ]
        )
        report = ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        self.assertEqual(2, report.sources["teamup"].payload["pages"])
        self.assertEqual(2, len(repo.load_occurrences(self.conn)))

    def test_the_client_only_ever_issues_read_only_gets(self):
        client, transport = ig.teamup(
            pages=[page([ig.teamup_event("occ1", "2025-07-01T18:00:00+03:00", "2025-07-01T20:00:00+03:00")])],
            calendar_key="SECRET-CALENDAR-KEY-77",
        )
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        for request in transport.requests:
            self.assertEqual("GET", request.method)
        self.assertNotIn("SECRET-CALENDAR-KEY-77", repo.current_release(self.conn)["payload"])
        self.assertNotIn("SECRET-CALENDAR-KEY-77", repr(client))

    def test_a_finished_event_is_not_required_to_be_confirmed_by_an_organiser(self):
        client, _ = ig.teamup(
            pages=[
                page(
                    [
                        ig.teamup_event("occ_done", "2025-07-20T18:00:00+03:00", "2025-07-20T20:00:00+03:00", title="Открытая лекция"),
                        ig.teamup_event("occ_future", "2025-08-20T18:00:00+03:00", "2025-08-20T20:00:00+03:00", title="Будущее"),
                    ]
                )
            ]
        )
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        payload = self.events_payload()
        self.assertEqual(1, payload["metrics"]["total_events"])
        self.assertEqual("2025-07-20", payload["public_events"][0]["on"])


if __name__ == "__main__":
    unittest.main()
