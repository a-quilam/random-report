"""Calendar: meetings vs other closed records, exact titles, exclusion, e2e.

The owner's decision under test:

* a completed record of the **allowed closed contour** (``meeting_subcalendars``)
  whose title is **exactly** «Собрания» is a ``meeting`` — counted once, and in the
  public calendar only its Moscow start day plus the constant «Собрание»; no raw
  title, id, time, url, description or people;
* the other closed records stay aggregate-only, split into a separate ``closed_rent``
  category by mapping (not every closed record is a rent);
* an excluded subcalendar gives neither a list nor a total;
* conflict/uncertainty is resolved conservatively (exact title match, most
  restrictive wins) — a substring or a longer title is never a meeting;
* everything is driven by a configuration of stable placeholder IDs.

All fixtures are synthetic: no real calendar key, subcalendar id, title or payload.
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

from randreport.domain.dto import EventsDTO, PublicMeetingDTO  # noqa: E402
from randreport.domain.events import (  # noqa: E402
    DEFAULT_MEETING_LABEL,
    EventType,
    event_mapping_from_env,
    event_stats,
    public_calendar_days,
    public_meetings,
    select_completed,
)
from randreport.domain.timeframes import month_period  # noqa: E402
from randreport.projection.csv_export import export_categories_csv  # noqa: E402
from randreport.projection.privacy import assert_no_leaks, find_leaks, public_json  # noqa: E402
from randreport.projection.release import ReleaseContext, build_events_payload  # noqa: E402
from randreport.store import apply_migrations, connect  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from randreport.web import build as web_build  # noqa: E402
from randreport.web import render  # noqa: E402
from randreport.web.release_index import ReleaseIndex  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as ing  # noqa: E402

CUTOFF = fx.msk(2025, 7, 31, 3, 0)
PERIOD = "2025-07"
MEETING_TITLE = "Собрания"
MEETING_DAY = "2025-07-19"


def dataset() -> list:
    """A closed contour with one real meeting, a substring look-alike, a rent and more."""

    return [
        fx.occurrence("occ_open_1", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), subcalendars=("sub_open",), title="Открытый вечер"),
        # the allowed closed contour + the exact title -> meeting
        fx.occurrence("occ_meeting_1", fx.msk(2025, 7, 19, 19, 0), fx.msk(2025, 7, 19, 21, 0), subcalendars=("sub_meetings",), title=MEETING_TITLE),
        # a longer title inside the contour is NOT a meeting (conservative)
        fx.occurrence("occ_meeting_like", fx.msk(2025, 7, 20, 19, 0), fx.msk(2025, 7, 20, 21, 0), subcalendars=("sub_meetings",), title="Общее собрание коммуны"),
        # a closed record mapped as a rent -> its own category
        fx.occurrence("occ_rent_1", fx.msk(2025, 7, 21, 12, 0), fx.msk(2025, 7, 21, 16, 0), subcalendars=("sub_rent",), title="Аренда зала: день рождения"),
        # an ordinary closed record (not a rent, not a meeting) -> aggregate only
        fx.occurrence("occ_closed_1", fx.msk(2025, 7, 22, 12, 0), fx.msk(2025, 7, 22, 16, 0), subcalendars=("sub_closed",), title="Закрытое: встреча арендаторов подвала"),
        # an excluded subcalendar -> neither a list nor a total
        fx.occurrence("occ_excluded_1", fx.msk(2025, 7, 23, 12, 0), fx.msk(2025, 7, 23, 16, 0), subcalendars=("sub_excluded",), title=MEETING_TITLE),
        # the exact title but a different (internal) contour -> not a public meeting
        fx.occurrence("occ_internal_1", fx.msk(2025, 7, 24, 12, 0), fx.msk(2025, 7, 24, 13, 0), subcalendars=("sub_internal",), title=MEETING_TITLE),
    ]


def selection():
    return select_completed(dataset(), mapping=fx.event_mapping(), cutoff=CUTOFF)


def ctx() -> ReleaseContext:
    return ReleaseContext(
        release_id="rel_20250731_0001",
        as_of=fx.msk(2025, 7, 31, 3, 30),
        period=month_period(PERIOD),
        freshness="fresh",
        verification="operational",
        completeness="complete",
        source_labels=("teamup",),
    )


class TestMeetingClassification(unittest.TestCase):
    def test_exact_title_in_the_contour_is_a_meeting_and_is_counted_once(self):
        chosen = selection()
        meetings = [c for c in chosen.completed if c.event_type == EventType.MEETING]
        self.assertEqual(1, len(meetings))
        self.assertEqual("occ_meeting_1", meetings[0].occurrence.occurrence_id)
        self.assertEqual(MEETING_DAY, meetings[0].occurrence.day().isoformat())
        self.assertEqual(DEFAULT_MEETING_LABEL, meetings[0].public_label)
        stats = event_stats(chosen, cutoff=CUTOFF)
        self.assertEqual(1, stats.meeting_total)
        self.assertEqual(1, stats.by_type["meeting"])
        self.assertEqual(6, stats.total)  # the excluded record is not counted at all

    def test_a_longer_title_in_the_contour_is_not_a_meeting(self):
        chosen = selection()
        like = [c for c in chosen.completed if c.occurrence.occurrence_id == "occ_meeting_like"][0]
        self.assertEqual(EventType.CLOSED, like.event_type)
        self.assertFalse(like.public)
        # a near miss is surfaced to admins instead of being guessed away
        self.assertIn("occ_meeting_like", chosen.admin_flags)

    def test_the_exact_title_outside_the_contour_stays_its_own_type(self):
        chosen = selection()
        internal = [c for c in chosen.completed if c.occurrence.occurrence_id == "occ_internal_1"][0]
        self.assertEqual(EventType.INTERNAL_MEETING, internal.event_type)

    def test_a_record_in_both_the_meeting_contour_and_rent_is_resolved_conservatively(self):
        mapping = fx.event_mapping()
        occurrences = [
            fx.occurrence(
                "occ_conflict",
                fx.msk(2025, 7, 19, 19, 0),
                fx.msk(2025, 7, 19, 21, 0),
                subcalendars=("sub_meetings", "sub_rent"),
                title=MEETING_TITLE,
            )
        ]
        chosen = select_completed(occurrences, mapping=mapping, cutoff=CUTOFF)
        self.assertEqual(EventType.CLOSED_RENT, chosen.completed[0].event_type)
        self.assertEqual((), public_meetings(chosen))
        self.assertIn("occ_conflict", chosen.admin_flags)

    def test_an_excluded_subcalendar_beats_the_meeting_contour(self):
        mapping = fx.event_mapping(excluded_subcalendars=frozenset({"sub_meetings"}))
        occurrences = [
            fx.occurrence("occ_both", fx.msk(2025, 7, 19, 19, 0), fx.msk(2025, 7, 19, 21, 0), subcalendars=("sub_meetings",), title=MEETING_TITLE)
        ]
        chosen = select_completed(occurrences, mapping=mapping, cutoff=CUTOFF)
        self.assertEqual((), chosen.completed)
        self.assertEqual(["occ_both"], [o.occurrence_id for o in chosen.excluded])
        self.assertEqual(0, event_stats(chosen, cutoff=CUTOFF).total)


class TestClosedRentSplitAndExclusion(unittest.TestCase):
    def test_not_every_closed_record_is_a_rent(self):
        stats = event_stats(selection(), cutoff=CUTOFF)
        # two closed records: the substring look-alike and the ordinary closed one;
        # only the rent-mapped one is a ``closed_rent``
        self.assertEqual(2, stats.by_type["closed"])
        self.assertEqual(1, stats.by_type["closed_rent"])
        self.assertEqual(3, stats.closed_total)

    def test_excluded_gives_neither_lists_nor_totals(self):
        chosen = selection()
        stats = event_stats(chosen, cutoff=CUTOFF)
        self.assertEqual(["occ_excluded_1"], [o.occurrence_id for o in chosen.excluded])
        self.assertNotIn("occ_excluded_1", [c.occurrence.occurrence_id for c in chosen.completed])
        self.assertEqual(6, stats.total)
        self.assertEqual({"2025-07-03": 1}, dict(public_calendar_days(chosen)))


class TestPublicProjection(unittest.TestCase):
    def setUp(self):
        self.payload = build_events_payload(ctx(), selection())

    def test_a_meeting_is_published_as_day_plus_constant_only(self):
        self.assertEqual(
            [{"on": MEETING_DAY, "label": "Собрание"}],
            [m.to_public_dict() for m in self.payload.meetings],
        )
        self.assertEqual({MEETING_DAY: 1}, dict(self.payload.meeting_days))

    def test_no_source_detail_of_a_meeting_reaches_the_payload(self):
        text = public_json(self.payload)
        for forbidden in (
            MEETING_TITLE,
            "Общее собрание коммуны",
            "Аренда зала: день рождения",
            "occ_meeting_1",
            "occ_rent_1",
            "sub_meetings",
            "sub_rent",
            "sub_excluded",
            "19:00",
            "2025-07-19T",
            "cal_main",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("Собрание", text)
        assert_no_leaks(self.payload.to_public_dict())
        self.assertEqual((), find_leaks(self.payload.to_public_dict()))

    def test_closed_rent_stays_aggregate_only_and_the_grid_hides_it(self):
        self.assertEqual({MEETING_DAY: 1}, dict(self.payload.meeting_days))
        self.assertNotIn("2025-07-21", self.payload.calendar_days)  # the rent leaves no mark
        self.assertNotIn("2025-07-21", self.payload.meeting_days)
        self.assertNotIn("2025-07-23", self.payload.calendar_days)  # the excluded record too
        self.assertEqual(1, self.payload.event_type_aggregates["closed_rent"])
        self.assertEqual(1, self.payload.event_type_aggregates["meeting"])

    def test_events_dto_round_trips_through_the_public_contract(self):
        data = self.payload.to_public_dict()
        self.assertEqual(self.payload, EventsDTO.from_public_dict(data))

    def test_the_payload_keeps_only_the_constant_not_the_source_title(self):
        # a distinctive configured meeting title makes the substitution observable:
        # the record's own title must never appear, the constant must
        mapping = fx.event_mapping(meeting_titles=frozenset({"Планёрка коммуны"}))
        occurrences = [
            fx.occurrence("occ_named", fx.msk(2025, 7, 19, 19, 0), fx.msk(2025, 7, 19, 21, 0), subcalendars=("sub_meetings",), title="Планёрка коммуны")
        ]
        chosen = select_completed(occurrences, mapping=mapping, cutoff=CUTOFF)
        payload = build_events_payload(ctx(), chosen)
        self.assertEqual([{"on": MEETING_DAY, "label": "Собрание"}], [m.to_public_dict() for m in payload.meetings])
        text = public_json(payload)
        self.assertNotIn("Планёрка коммуны", text)
        self.assertIn("Собрание", text)
        html = render.events_section(payload, period_label=PERIOD, historical={})
        self.assertNotIn("Планёрка коммуны", html)
        self.assertIn("Собрание", html)

    def test_a_meeting_dto_has_no_other_field(self):
        import dataclasses

        names = {field.name for field in dataclasses.fields(PublicMeetingDTO)}
        self.assertEqual({"on", "label"}, names)

    def test_the_rendered_calendar_marks_the_meeting_day(self):
        html = render.events_section(self.payload, period_label=PERIOD, historical={})
        self.assertIn("Собрание", html)
        self.assertIn(MEETING_DAY, html)
        self.assertNotIn("Общее собрание коммуны", html)
        self.assertNotIn("Аренда зала: день рождения", html)
        self.assertNotIn("occ_meeting_1", html)
        self.assertNotIn("occ_rent_1", html)
        self.assertNotIn("19:00", html)


class TestConfiguration(unittest.TestCase):
    def test_the_environment_configuration_uses_placeholder_ids(self):
        env = {
            "TEAMUP_SUBCALENDAR_TYPES": "ph-meetings=closed,ph-open=open",
            "TEAMUP_PRIVATE_SUBCALENDARS": "ph-meetings",
            "TEAMUP_EXCLUDED_SUBCALENDARS": "ph-excluded",
            "TEAMUP_MEETING_SUBCALENDARS": "ph-meetings",
            "TEAMUP_RENT_SUBCALENDARS": "ph-rent",
            "TEAMUP_MEETING_TITLES": "Собрания;Общее собрание",
            "TEAMUP_MEETING_LABEL": "Собрание",
        }
        mapping = event_mapping_from_env(env)
        self.assertEqual(frozenset({"ph-excluded"}), mapping.excluded_subcalendars)
        self.assertEqual(frozenset({"ph-meetings"}), mapping.meeting_subcalendars)
        self.assertEqual(frozenset({"ph-rent"}), mapping.rent_subcalendars)
        self.assertEqual(frozenset({"Собрания", "Общее собрание"}), mapping.meeting_titles)
        self.assertEqual("Собрание", mapping.meeting_label)

    def test_defaults_promote_nothing(self):
        mapping = event_mapping_from_env({})
        self.assertEqual(frozenset(), mapping.meeting_subcalendars)
        self.assertEqual(frozenset(), mapping.rent_subcalendars)
        self.assertEqual(frozenset(), mapping.excluded_subcalendars)
        self.assertEqual(frozenset({MEETING_TITLE}), mapping.meeting_titles)
        self.assertEqual("exact", mapping.meeting_match)

    def test_the_configured_match_mode_travels_with_the_mapping(self):
        mapping = event_mapping_from_env(
            {
                "TEAMUP_MEETING_SUBCALENDARS": "ph-meetings",
                "TEAMUP_MEETING_TITLES": "собрание;собрания",
                "TEAMUP_MEETING_MATCH": "contains",
            }
        )
        self.assertEqual("contains", mapping.meeting_match)
        self.assertEqual(
            EventType.MEETING,
            mapping.closed_kind(("ph-meetings",), "Общее собрание коммуны"),
        )
        with self.assertRaises(ValueError):
            event_mapping_from_env({"TEAMUP_MEETING_MATCH": "substring"})

    def test_a_mapping_payload_carries_the_match_mode(self):
        payload = {
            "subcalendar_types": {"ph-meetings": "closed"},
            "meeting_subcalendars": ["ph-meetings"],
            "meeting_titles": ["собрание", "собрания"],
            "meeting_label": "Собрание",
            "meeting_match": "contains",
        }
        mapping = web_build.event_mapping_from_payload("rules-v4", payload)
        self.assertEqual("contains", mapping.meeting_match)
        self.assertEqual(EventType.MEETING, mapping.closed_kind(("ph-meetings",), "СОБРАНИЕ коммунаров"))
        bad = dict(payload, meeting_match="fuzzy")
        with self.assertRaises(ValueError):
            web_build.event_mapping_from_payload("rules-v4", bad)

    def test_the_example_configuration_file_matches_the_payload_shape(self):
        example = json.loads((ROOT / "config" / "calendar_mapping.example.json").read_text(encoding="utf-8"))
        mapping = web_build.event_mapping_from_payload("example", example)
        self.assertIn("placeholder-sub-meetings", mapping.meeting_subcalendars)
        self.assertIn("placeholder-sub-rent", mapping.rent_subcalendars)
        self.assertIn("placeholder-sub-excluded", mapping.excluded_subcalendars)
        # every stable id in the example is a placeholder, never a real value
        ids = list(example["subcalendar_types"]) + list(example["private_subcalendars"]) + list(example["excluded_subcalendars"]) + list(example["meeting_subcalendars"]) + list(example["rent_subcalendars"])
        self.assertTrue(ids)
        self.assertTrue(all(item.startswith("placeholder-") for item in ids))

    def test_an_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            web_build.event_mapping_from_payload("bad", {"subcalendar_types": {"ph": "meeting"}})


class TestDeskConfiguration(unittest.TestCase):
    def setUp(self):
        self.conn = connect()
        apply_migrations(self.conn)
        self.addCleanup(self.conn.close)

    def test_the_desk_records_the_closed_contour_roles(self):
        from randreport.admin import desk

        version = desk.set_event_mapping(
            self.conn,
            subcalendar_id="ph-meetings",
            event_type="closed",
            private=True,
            actor="tester",
            excluded=False,
            meeting_contour=True,
            rent=False,
            now="2025-07-31T00:00:00+00:00",
        )
        self.assertTrue(version)
        loaded = repo.load_event_mapping(self.conn)
        assert loaded is not None
        _version, payload = loaded
        self.assertEqual(["ph-meetings"], list(payload["meeting_subcalendars"]))
        self.assertEqual([], list(payload["rent_subcalendars"]))
        self.assertEqual([], list(payload["excluded_subcalendars"]))
        view = desk.settings_view(self.conn)
        self.assertIn("ph-meetings", view["event_contour"]["meeting"])


class TestEndToEndTeamup(unittest.TestCase):
    """Synthetic Teamup -> store -> release JSON/calendar/CSV, with deletions."""

    def _page(self, *, delete_meeting: bool = False) -> dict:
        return {
            "events": [
                ing.teamup_event("e_open", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", subcalendars=("sub_open",), title="Открытый вечер"),
                ing.teamup_event(
                    "e_meeting",
                    "2025-07-19T19:00:00+03:00",
                    "2025-07-19T21:00:00+03:00",
                    subcalendars=("sub_meetings",),
                    title=MEETING_TITLE,
                    delete=delete_meeting,
                ),
                ing.teamup_event("e_like", "2025-07-20T19:00:00+03:00", "2025-07-20T21:00:00+03:00", subcalendars=("sub_meetings",), title="Итоги: СОБРАНИЯ!"),
                ing.teamup_event("e_rent", "2025-07-21T12:00:00+03:00", "2025-07-21T16:00:00+03:00", subcalendars=("sub_rent",), title="Аренда зала"),
                ing.teamup_event("e_closed", "2025-07-22T12:00:00+03:00", "2025-07-22T16:00:00+03:00", subcalendars=("sub_closed",), title="Закрытая встреча"),
                ing.teamup_event("e_excluded", "2025-07-23T12:00:00+03:00", "2025-07-23T16:00:00+03:00", subcalendars=("sub_excluded",), title=MEETING_TITLE),
            ],
            "pages": {"next": None},
        }

    def _conn(self):
        conn = connect()
        apply_migrations(conn)
        self.addCleanup(conn.close)
        return conn

    def test_teamup_to_report_calendar_json_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, _transport = ing.teamup(pages=[self._page(), self._page()])
            conn = self._conn()
            report = ing.run_once(
                conn,
                [ing.teamup_adapter(client)],
                release_builder=ing.builder(PERIOD),
                publish_dir=str(root / "releases"),
                lease=str(root / "nightly.lock"),
            )
            self.assertTrue(report.published)
            release_id = report.release_id
            self.assertIsNotNone(release_id)
            assert release_id is not None

            release = ReleaseIndex(root / "releases").current()
            self.assertIsNotNone(release)
            assert release is not None
            events = release.events
            self.assertIsNotNone(events)
            assert events is not None
            self.assertEqual([{"on": MEETING_DAY, "label": "Собрание"}], [m.to_public_dict() for m in events.meetings])
            self.assertEqual({MEETING_DAY: 1}, dict(events.meeting_days))
            self.assertEqual(1, events.event_type_aggregates["meeting"])
            self.assertEqual(1, events.event_type_aggregates["closed_rent"])
            self.assertEqual(2, events.event_type_aggregates["closed"])

            published = (root / "releases" / release_id / "events.json").read_text(encoding="utf-8")
            for forbidden in (MEETING_TITLE, "Итоги: СОБРАНИЯ!", "e_meeting", "e_rent", "sub_meetings", "19:00", "cal_test", "2025-07-19T"):
                self.assertNotIn(forbidden, published)
            self.assertIn("Собрание", published)

            html = render.events_section(events, period_label=PERIOD, historical={})
            self.assertIn("Собрание", html)
            self.assertNotIn("Итоги: СОБРАНИЯ!", html)
            self.assertNotIn("Аренда зала", html)
            self.assertNotIn("Закрытая встреча", html)

            money = release.money
            self.assertIsNotNone(money)
            assert money is not None
            csv_text = export_categories_csv(money, params={"period": PERIOD})
            self.assertNotIn(MEETING_TITLE, csv_text)
            self.assertNotIn(MEETING_DAY, csv_text)

    def test_an_explicit_deletion_removes_the_meeting_from_the_next_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, _transport = ing.teamup(pages=[self._page(), self._page(delete_meeting=True)])
            conn = self._conn()

            first = ing.run_once(
                conn,
                [ing.teamup_adapter(client)],
                release_builder=ing.builder(PERIOD),
                publish_dir=str(root / "releases"),
                lease=str(root / "nightly.lock"),
            )
            self.assertTrue(first.published)
            current = ReleaseIndex(root / "releases").current()
            self.assertIsNotNone(current)
            assert current is not None
            first_events = current.events
            assert first_events is not None
            self.assertEqual(1, len(first_events.meetings))

            second = ing.run_once(
                conn,
                [ing.teamup_adapter(client)],
                release_builder=ing.builder(PERIOD),
                publish_dir=str(root / "releases"),
                lease=str(root / "nightly.lock"),
            )
            self.assertTrue(second.published)
            rebuilt_release = ReleaseIndex(root / "releases").current()
            self.assertIsNotNone(rebuilt_release)
            assert rebuilt_release is not None
            rebuilt = rebuilt_release.events
            assert rebuilt is not None
            self.assertEqual((), rebuilt.meetings)
            self.assertEqual({}, dict(rebuilt.meeting_days))
            self.assertEqual(0, rebuilt.event_type_aggregates["meeting"])
            # the deletion is kept in the private revision, not lost
            row = conn.execute("SELECT deleted FROM calendar_occurrences WHERE occurrence_id = 'e_meeting'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(1, int(row["deleted"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
