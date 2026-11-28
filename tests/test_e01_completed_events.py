"""E01 — completed events: cutoff, night/multi-day, all-day, Moscow date boundary."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.events import (  # noqa: E402
    event_stats,
    public_calendar_days,
    select_completed,
)
from randreport.domain.timeframes import month_period, parse_period  # noqa: E402
from randreport.ops.pipeline import ReleaseBuilder  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402

CUTOFF = fx.msk(2025, 7, 15, 3, 0)
#: after every event of the fixtures, so "completed" is not what is under test here
AFTER_ALL = fx.msk(2026, 1, 1, 3, 0)


class EventTestCase(unittest.TestCase):
    MAPPING = fx.event_mapping()

    def select(self, occurrences, cutoff=CUTOFF):
        return select_completed(occurrences, mapping=self.MAPPING, cutoff=cutoff)


class TestE01Completion(EventTestCase):
    def test_future_and_ongoing_are_excluded(self):
        occurrences = [
            fx.occurrence("occ_done", fx.msk(2025, 7, 14, 18, 0), fx.msk(2025, 7, 14, 20, 0)),
            fx.occurrence("occ_future", fx.msk(2025, 7, 20, 18, 0), fx.msk(2025, 7, 20, 20, 0)),
            fx.occurrence("occ_ongoing", fx.msk(2025, 7, 15, 1, 0), fx.msk(2025, 7, 15, 5, 0)),
        ]
        selection = self.select(occurrences)
        self.assertEqual(["occ_done"], [c.occurrence.occurrence_id for c in selection.completed])
        self.assertEqual(["occ_future"], [o.occurrence_id for o in selection.future])
        self.assertEqual(["occ_ongoing"], [o.occurrence_id for o in selection.ongoing])
        self.assertEqual(1, event_stats(selection, cutoff=CUTOFF).total)

    def test_event_ending_exactly_at_cutoff_counts_as_completed(self):
        occurrences = [fx.occurrence("occ_edge", fx.msk(2025, 7, 14, 20, 0), fx.msk(2025, 7, 15, 3, 0))]
        selection = self.select(occurrences)
        self.assertEqual(["occ_edge"], [c.occurrence.occurrence_id for c in selection.completed])

    def test_night_event_is_counted_once_on_its_start_day(self):
        occurrences = [
            fx.occurrence("occ_night", fx.msk(2025, 7, 10, 22, 0), fx.msk(2025, 7, 11, 2, 0)),
        ]
        selection = self.select(occurrences)
        self.assertEqual(1, event_stats(selection, cutoff=CUTOFF).total)
        self.assertEqual({"2025-07-10": 1}, dict(public_calendar_days(selection)))

    def test_multi_day_event_stays_a_single_record(self):
        occurrences = [
            fx.occurrence("occ_long", fx.msk(2025, 7, 5, 18, 0), fx.msk(2025, 7, 8, 12, 0)),
        ]
        selection = self.select(occurrences)
        self.assertEqual(1, event_stats(selection, cutoff=CUTOFF).total)
        self.assertEqual({"2025-07-05": 1}, dict(public_calendar_days(selection)))

    def test_all_day_event_uses_the_exclusive_end_from_the_api(self):
        occurrence = fx.occurrence(
            "occ_allday",
            fx.msk(2025, 7, 10, 0, 0),
            fx.msk(2025, 7, 11, 0, 0),
            all_day=True,
        )
        completed = self.select([occurrence], cutoff=fx.msk(2025, 7, 11, 3, 0))
        self.assertEqual(1, event_stats(completed, cutoff=fx.msk(2025, 7, 11, 3, 0)).total)
        ongoing = self.select([occurrence], cutoff=fx.msk(2025, 7, 10, 23, 0))
        self.assertEqual(0, event_stats(ongoing, cutoff=fx.msk(2025, 7, 10, 23, 0)).total)
        self.assertEqual(["occ_allday"], [o.occurrence_id for o in ongoing.ongoing])

    def test_moscow_boundary_decides_the_day_not_the_host_timezone(self):
        occurrence = fx.occurrence(
            "occ_boundary",
            fx.utc(2025, 12, 31, 22, 30),  # 2026-01-01 01:30 MSK
            fx.utc(2025, 12, 31, 23, 30),
        )
        cutoff = fx.msk(2026, 1, 5, 3, 0)
        selection = self.select([occurrence], cutoff=cutoff)
        self.assertEqual(1, event_stats(selection, cutoff=cutoff).total)
        self.assertEqual("2026-01-01", selection.completed[0].occurrence.day().isoformat())
        self.assertTrue(month_period("2026-01").contains(selection.completed[0].occurrence.day()))
        self.assertFalse(month_period("2025-12").contains(selection.completed[0].occurrence.day()))

    def test_unknown_duration_is_not_counted_as_completed(self):
        occurrences = [fx.occurrence("occ_noduration", fx.msk(2025, 7, 14, 18, 0), None)]
        selection = self.select(occurrences)
        self.assertEqual(0, event_stats(selection, cutoff=CUTOFF).total)
        self.assertEqual(["occ_noduration"], [o.occurrence_id for o in selection.unknown_end])

    def test_public_share_counts_only_public_types(self):
        occurrences = [
            fx.occurrence("occ_open", fx.msk(2025, 7, 1, 18, 0), fx.msk(2025, 7, 1, 20, 0)),
            fx.occurrence(
                "occ_closed",
                fx.msk(2025, 7, 2, 18, 0),
                fx.msk(2025, 7, 2, 20, 0),
                subcalendars=("sub_closed",),
            ),
        ]
        stats = event_stats(self.select(occurrences), cutoff=CUTOFF)
        self.assertEqual(2, stats.total)
        self.assertEqual(1, stats.public_total)
        self.assertEqual(1, stats.closed_total)


class TestPeriodScopedRelease(unittest.TestCase):
    """A release reports the occurrences of *its* period, not the whole window.

    The staging run exposed the defect: 38 releases of 38 different periods all
    carried the same all-time calendar numbers (457 events, 54 meetings), because
    the builder passed every stored occurrence to the projection. A monthly or
    yearly release must describe its own period (docs/REQUIREMENTS.md section 3.3);
    an occurrence belongs to the period of its Moscow start day.
    """

    def setUp(self):
        self.conn = ig.fresh_conn()
        self.addCleanup(self.conn.close)
        repo.upsert_occurrences(
            self.conn,
            [
                fx.occurrence("occ_july", fx.msk(2025, 7, 10, 18, 0), fx.msk(2025, 7, 10, 20, 0)),
                # a night event belongs to the day it starts on
                fx.occurrence("occ_july_night", fx.msk(2025, 7, 31, 23, 0), fx.msk(2025, 8, 1, 1, 0)),
                fx.occurrence("occ_august", fx.msk(2025, 8, 3, 18, 0), fx.msk(2025, 8, 3, 20, 0)),
                fx.occurrence(
                    "occ_august_meeting",
                    fx.msk(2025, 8, 19, 19, 0),
                    fx.msk(2025, 8, 19, 21, 0),
                    subcalendars=("sub_meetings",),
                    title="Собрания",
                ),
            ],
        )

    def _events(self, period: str) -> dict:
        builder = ReleaseBuilder(
            policy=fx.policy(),
            event_mapping=fx.event_mapping(),
            period=parse_period(period),
        )
        release = builder.build(self.conn, cutoff=AFTER_ALL, freshness="fresh")
        return json.loads(release.payload_json["events"])

    def test_a_monthly_release_counts_only_its_month(self):
        july = self._events("2025-07")
        august = self._events("2025-08")
        self.assertEqual(2, july["metrics"]["total_events"])
        self.assertEqual(0, july["metrics"]["meeting_aggregate"])
        self.assertEqual(2, august["metrics"]["total_events"])
        self.assertEqual(1, august["metrics"]["meeting_aggregate"])
        self.assertEqual(["2025-08-19"], [row["on"] for row in august["meetings"]])

    def test_a_yearly_release_counts_the_whole_year(self):
        year = self._events("2025")
        self.assertEqual(4, year["metrics"]["total_events"])
        self.assertEqual(1, year["metrics"]["meeting_aggregate"])

    def test_a_period_without_events_reports_confirmed_zeros(self):
        other = self._events("2026-01")
        self.assertEqual(0, other["metrics"]["total_events"])
        self.assertEqual(0, other["metrics"]["meeting_aggregate"])
        self.assertEqual([], list(other["meetings"]))


if __name__ == "__main__":
    unittest.main()
