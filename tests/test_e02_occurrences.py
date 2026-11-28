"""E02 — occurrences: recurrence, several calendars, moves, deletions, retention, unknown type."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.events import (  # noqa: E402
    EventType,
    event_stats,
    public_calendar_days,
    select_completed,
)
from tests import fixtures as fx  # noqa: E402

CUTOFF = fx.msk(2025, 7, 31, 3, 0)


class TestE02Occurrences(unittest.TestCase):
    MAPPING = fx.event_mapping()

    def select(self, occurrences):
        return select_completed(occurrences, mapping=self.MAPPING, cutoff=CUTOFF)

    def test_recurring_occurrences_are_counted_per_instance(self):
        occurrences = [
            fx.occurrence(f"occ_series_{index}", fx.msk(2025, 7, 5 + index, 18, 0), fx.msk(2025, 7, 5 + index, 20, 0), series_id="ser_1")
            for index in range(3)
        ]
        stats = event_stats(self.select(occurrences), cutoff=CUTOFF)
        self.assertEqual(3, stats.total)

    def test_record_in_several_subcalendars_counts_once(self):
        occurrences = [
            fx.occurrence(
                "occ_multi",
                fx.msk(2025, 7, 3, 18, 0),
                fx.msk(2025, 7, 3, 20, 0),
                subcalendars=("sub_open", "sub_preparation"),
            )
        ]
        selection = self.select(occurrences)
        stats = event_stats(selection, cutoff=CUTOFF)
        self.assertEqual(1, stats.total)
        self.assertEqual(EventType.PREPARATION, selection.completed[0].event_type)

    def test_move_replaces_the_occurrence_without_creating_a_duplicate(self):
        occurrences = [
            fx.occurrence("occ_move", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="ffffffff"),
            fx.occurrence("occ_move", fx.msk(2025, 7, 4, 19, 0), fx.msk(2025, 7, 4, 21, 0), revision="00000001"),
        ]
        selection = self.select(occurrences)
        self.assertEqual(1, event_stats(selection, cutoff=CUTOFF).total)
        self.assertEqual("2025-07-04", selection.completed[0].occurrence.start.astimezone(fx.MSK).date().isoformat())
        self.assertEqual(["ffffffff"], [o.revision for o in selection.superseded])
        self.assertEqual(2, len(selection.revision_log))
        self.assertEqual(1, selection.duplicates_collapsed)

    def test_explicit_deletion_leaves_operational_stats_but_keeps_the_revision(self):
        occurrences = [
            fx.occurrence("occ_gone", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="ffffffff"),
            fx.occurrence(
                "occ_gone",
                fx.msk(2025, 7, 3, 18, 0),
                fx.msk(2025, 7, 3, 20, 0),
                revision="00000001",
                deleted=True,
            ),
        ]
        selection = self.select(occurrences)
        self.assertEqual(0, event_stats(selection, cutoff=CUTOFF).total)
        self.assertEqual(["occ_gone"], [o.occurrence_id for o in selection.deleted])
        self.assertEqual(2, len(selection.revision_log))

    def test_retention_disappearance_does_not_erase_the_local_archive(self):
        occurrences = [
            fx.occurrence(
                "occ_archived",
                fx.msk(2025, 7, 6, 18, 0),
                fx.msk(2025, 7, 6, 20, 0),
                source_present=False,
                local_archive=True,
            )
        ]
        selection = self.select(occurrences)
        stats = event_stats(selection, cutoff=CUTOFF)
        self.assertEqual(1, stats.total)
        self.assertEqual(1, stats.local_archive_total)
        self.assertEqual(["occ_archived"], [o.occurrence_id for o in selection.local_archive_only])

    def test_unknown_type_is_counted_but_not_treated_as_open(self):
        occurrences = [
            fx.occurrence(
                "occ_lecture",
                fx.msk(2025, 7, 7, 19, 0),
                fx.msk(2025, 7, 7, 21, 0),
                subcalendars=("sub_lecture",),
                title="Лекция",
            )
        ]
        selection = self.select(occurrences)
        stats = event_stats(selection, cutoff=CUTOFF)
        self.assertEqual(1, stats.total)
        self.assertEqual(1, stats.by_type["unknown"])
        self.assertEqual(0, stats.by_type["open"])
        payload = stats.public_payload()
        self.assertEqual(1, payload["event_type_aggregates"]["unknown"])
        self.assertEqual(0, payload["event_type_aggregates"]["open"])

    def test_type_and_privacy_come_from_subcalendar_mapping_not_from_the_title(self):
        occurrences = [
            fx.occurrence(
                "occ_cancel_word",
                fx.msk(2025, 7, 8, 18, 0),
                fx.msk(2025, 7, 8, 20, 0),
                subcalendars=("sub_open",),
                title="Отмена: репетиция арендаторов",
            )
        ]
        selection = self.select(occurrences)
        self.assertEqual(EventType.OPEN, selection.completed[0].event_type)
        self.assertEqual(1, event_stats(selection, cutoff=CUTOFF).total)
        self.assertIn("occ_cancel_word", selection.admin_flags)

    def test_conflicting_subcalendars_prefer_privacy(self):
        occurrences = [
            fx.occurrence(
                "occ_conflict",
                fx.msk(2025, 7, 9, 18, 0),
                fx.msk(2025, 7, 9, 20, 0),
                subcalendars=("sub_open", "sub_closed"),
            )
        ]
        selection = self.select(occurrences)
        self.assertFalse(selection.completed[0].public)
        self.assertEqual(EventType.CLOSED, selection.completed[0].event_type)
        self.assertEqual({}, dict(public_calendar_days(selection)))

    def test_non_event_calendars_are_kept_in_totals_not_dropped(self):
        occurrences = [
            fx.occurrence("occ_meeting", fx.msk(2025, 7, 10, 12, 0), fx.msk(2025, 7, 10, 13, 0), subcalendars=("sub_internal",)),
            fx.occurrence("occ_prep", fx.msk(2025, 7, 11, 12, 0), fx.msk(2025, 7, 11, 13, 0), subcalendars=("sub_preparation",)),
            fx.occurrence("occ_reg", fx.msk(2025, 7, 12, 12, 0), fx.msk(2025, 7, 12, 13, 0), subcalendars=("sub_registration",)),
        ]
        stats = event_stats(self.select(occurrences), cutoff=CUTOFF)
        self.assertEqual(3, stats.total)
        self.assertEqual(1, stats.by_type["internal_meeting"])
        self.assertEqual(1, stats.by_type["preparation"])
        self.assertEqual(1, stats.by_type["open_by_registration"])
        self.assertEqual(1, stats.public_total)


if __name__ == "__main__":
    unittest.main()
