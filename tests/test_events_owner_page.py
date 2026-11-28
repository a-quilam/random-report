"""The owner-approved events poster (route /events): data wiring and privacy.

The composition belongs to the approved prototype; these tests pin the data
contract (03_DATA_AND_PRIVACY.md): public events only, closed contour as one
derived aggregate, honest absence instead of zeros, no demo generator, CSP-safe
markup (no inline styles, no inline scripts).
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from randreport.domain.dto import (  # noqa: E402
    EventsDTO,
    EventTypeAggregateDTO,
    PeriodDTO,
    PublicEventDTO,
)
from randreport.web import events_owner  # noqa: E402
from randreport.web.app import CSP, PublicSite  # noqa: E402
from tests import dev_release  # noqa: E402


PERIOD = "2025-07"


def make_dto(*, as_of: date = date(2025, 7, 28), public=True, meetings_days: dict | None = None,
             granularity: str = "day", metrics: dict | None = None,
             details: tuple[EventTypeAggregateDTO, ...] | None = None) -> EventsDTO:
    """A small July fixture: two public events plus a closed contour."""

    period = PeriodDTO(requested=PERIOD, covered_start=date(2025, 7, 1),
                       covered_end=date(2025, 7, 31), complete=False)
    if public:
        public_events = (
            PublicEventDTO(on=date(2025, 7, 5), type="open", title="Открытый концерт",
                           description=None, announce_url=None, registration_open=False),
            PublicEventDTO(on=date(2025, 7, 12), type="open_by_registration",
                           title="Мастер-класс по записи", description="Приходите заранее.",
                           announce_url="https://t.me/randdom/123", registration_open=False),
        )
        days = {"2025-07-05": 1, "2025-07-12": 1}
    else:
        public_events = ()
        days = {}
    if details is None:
        details = (
            EventTypeAggregateDTO(type="open", count=1, public=True),
            EventTypeAggregateDTO(type="open_by_registration", count=1, public=True),
            EventTypeAggregateDTO(type="closed", count=2, public=False),
            EventTypeAggregateDTO(type="meeting", count=1, public=False),
        )
    if metrics is None:
        metrics = {"total_events": 6, "public_events": 2}
    return EventsDTO(
        period=period, as_of=as_of, release_id="test-release", freshness="fresh",
        verification="operational", completeness="partial", source_labels=("Teamup",),
        public_events=public_events, meetings=(),
        event_type_aggregates={"open": 1}, event_type_details=details,
        calendar_days=days, meeting_days=meetings_days or {}, calendar_granularity=granularity,
        metrics=metrics,
    )


def render_page(dto: EventsDTO | None, **kwargs) -> str:
    defaults = dict(period_label=PERIOD, month_options=[PERIOD, "2025-06"], events=dto,
                    prev_total=5, prev_month=6, photo_url=None, logo_url=None)
    defaults.update(kwargs)
    return events_owner.page(**defaults)


def payload_of(page_html: str) -> dict:
    match = re.search(r'<script type="application/json" id="events-data">(.*?)</script>', page_html, re.S)
    assert match, "events payload script element missing"
    return json.loads(match.group(1).replace("<\\/", "</"))


class HeroFiguresTests(unittest.TestCase):
    def test_hero_counts_come_from_metrics_not_from_public_events(self) -> None:
        html = render_page(make_dto())
        self.assertIn(">6</strong>", html)  # total, including closed
        self.assertIn("СОБЫТИЙ В ИЮЛЕ", html)

    def test_previous_month_line_uses_real_previous_figure(self) -> None:
        html = render_page(make_dto(), prev_total=5, prev_month=6)
        self.assertIn("<b>в июне:</b> 5", html)

    def test_missing_previous_month_is_absence_not_zero(self) -> None:
        html = render_page(make_dto(), prev_total=None, prev_month=None)
        self.assertIn("НЕТ СВЕРКИ", html)
        self.assertNotRegex(html, r"в июне:</b>\s*0")

    def test_hidden_aggregate_is_derived_sum(self) -> None:
        html = render_page(make_dto())
        match = re.search(r'<strong id="hiddenCount">(\d+)</strong>', html)
        self.assertEqual(match.group(1), "4")  # 6 total - 2 public

    def test_unknown_hidden_aggregate_does_not_print_zero(self) -> None:
        html = render_page(make_dto(metrics={"total_events": None, "public_events": 2}))
        self.assertIn("нет данных", html)

    def test_missing_release_keeps_honest_empty_state(self) -> None:
        html = render_page(None)
        self.assertIn("—</strong>", html)
        self.assertIn("ДАННЫХ ЗА ЭТОТ ПЕРИОД НЕТ", html)
        self.assertIn("ПУБЛИЧНЫЙ ВЫПУСК ЗА ПЕРИОД ЕЩЁ НЕ СФОРМИРОВАН", html)


class PrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = render_page(make_dto())
        self.payload = payload_of(self.html)

    def test_closed_types_are_absent_from_filters_and_breakdown(self) -> None:
        for closed in ("closed", "meeting", "internal_meeting", "preparation", "unknown"):
            self.assertNotIn(f'data-filter="{closed}"', self.html)
            self.assertNotIn(f"fill-{closed}", self.html)
        self.assertNotIn("ЗАКРЫТЫЕ</span>", self.html.replace("ЗАКРЫТЫЕ СОБЫТИЙ", ""))

    def test_payload_carries_public_projection_only(self) -> None:
        allowed = {"period", "year", "gen", "inMonth", "granularity", "first_offset",
                   "max_day", "limit", "total", "hidden", "prev", "types",
                   "selectedDay", "days", "events"}
        self.assertEqual(set(self.payload), allowed)
        for event in self.payload["events"]:
            self.assertEqual(set(event), {"d", "type", "title", "description", "url"})
            self.assertIn(event["type"], ("open", "open_by_registration"))

    def test_closed_titles_and_meeting_days_never_reach_html_or_payload(self) -> None:
        for token in ("Закрытая встреча", "Собрание", "meeting_days", "raw"):
            self.assertNotIn(token, self.html)
            self.assertNotIn(token, json.dumps(self.payload, ensure_ascii=False))

    def test_payload_never_contains_closed_type_keys(self) -> None:
        text = json.dumps(self.payload, ensure_ascii=False)
        for closed in ("closed", "internal_meeting", "preparation"):
            self.assertNotIn(f'"type":"{closed}"', text)
            self.assertNotIn(f'"key":"{closed}"', text)

    def test_unsafe_announce_url_is_dropped_and_safe_one_kept(self) -> None:
        dto = make_dto()
        safe = dto.public_events[1]
        attack = PublicEventDTO(on=date(2025, 7, 19), type="open", title="Атака",
                                description=None, announce_url="javascript:alert(1)",
                                registration_open=False)
        protocol_relative = PublicEventDTO(on=date(2025, 7, 20), type="open", title="Относительная",
                                           description=None, announce_url="//evil.example/x",
                                           registration_open=False)
        period = PeriodDTO(requested=PERIOD, covered_start=date(2025, 7, 1),
                           covered_end=date(2025, 7, 31), complete=False)
        events = (attack, protocol_relative, safe)
        days = {"2025-07-19": 1, "2025-07-20": 1, "2025-07-12": 1}
        dto = EventsDTO(
            period=period, as_of=date(2025, 7, 28), release_id="x", freshness="fresh",
            verification="operational", completeness="partial", source_labels=("Teamup",),
            public_events=events, meetings=(), event_type_aggregates={},
            event_type_details=(EventTypeAggregateDTO(type="open", count=3, public=True),),
            calendar_days=days, meeting_days={}, calendar_granularity="day",
            metrics={"total_events": 3, "public_events": 3},
        )
        html = render_page(dto)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("//evil.example", html)
        self.assertIn("https://t.me/randdom/123", html)

    def test_title_is_escaped_in_html_and_payload(self) -> None:
        period = PeriodDTO(requested=PERIOD, covered_start=date(2025, 7, 1),
                           covered_end=date(2025, 7, 31), complete=False)
        evil = PublicEventDTO(on=date(2025, 7, 5), type="open",
                              title='</script><script>alert(1)</script><i onclick="x()">',
                              description=None, announce_url=None, registration_open=False)
        dto = EventsDTO(
            period=period, as_of=date(2025, 7, 28), release_id="x", freshness="fresh",
            verification="operational", completeness="partial", source_labels=("Teamup",),
            public_events=(evil,), meetings=(), event_type_aggregates={},
            event_type_details=(EventTypeAggregateDTO(type="open", count=1, public=True),),
            calendar_days={"2025-07-05": 1}, meeting_days={}, calendar_granularity="day",
            metrics={"total_events": 1, "public_events": 1},
        )
        html = render_page(dto)
        self.assertNotIn("</script><script", html)
        self.assertNotIn('onclick="x()"', html)
        payload = payload_of(html)
        self.assertEqual(payload["events"][0]["title"], evil.title)  # escaped on output, intact in JSON

    def test_events_with_future_end_are_not_marked_held(self) -> None:
        # as_of mid-month: later days stay disabled and outside the report period
        html = render_page(make_dto(as_of=date(2025, 7, 10)))
        payload = payload_of(html)
        self.assertEqual(payload["limit"], 10)
        self.assertIn("class=\"day outside\"", html)
        self.assertIn("вне отчётного периода", html)
        # a public event of a day after the cutoff cannot exist in the payload
        for event in payload["events"]:
            self.assertLessEqual(event["d"], 10)

    def test_today_mark_is_only_on_the_data_date(self) -> None:
        html = render_page(make_dto(as_of=date(2025, 7, 28)))
        # the data date keeps its red underline even with no events that day
        self.assertIn('class="day today" aria-label="28 июля: 0 публичных событий"', html)

    def test_past_month_has_no_disabled_days(self) -> None:
        html = render_page(make_dto(as_of=date(2025, 8, 5)))
        self.assertNotIn("class=\"day outside\"", html)
        self.assertNotIn("вне отчётного периода", html)


class CompositionTests(unittest.TestCase):
    def test_filters_are_built_only_from_public_details(self) -> None:
        html = render_page(make_dto())
        self.assertIn('data-filter="open"', html)
        self.assertIn('data-filter="open_by_registration"', html)
        self.assertEqual(html.count('class="filter"'), 3)  # ВСЕ + two public types

    def test_fixture_public_types_render_through_production_code(self) -> None:
        details = tuple(
            EventTypeAggregateDTO(type=key, count=count, public=True)
            for key, count in (("lec", 7), ("kino", 6), ("igry", 6), ("art", 6), ("music", 6))
        )
        html = render_page(make_dto(details=details))
        for label in ("ЛЕКЦИИ", "КИНО", "ИГРЫ", "МАСТЕРСКИЕ", "МУЗЫКА"):
            self.assertIn(label, html)

    def test_aggregate_only_period_draws_no_fake_grid_details(self) -> None:
        html = render_page(make_dto(granularity="month", public=False))
        self.assertIn("дневная сетка не публикуется", html)
        self.assertNotIn('<i class="dot', html)
        payload = payload_of(html)
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["days"], {})

    def test_selected_day_defaults_to_last_day_with_events(self) -> None:
        html = render_page(make_dto())
        self.assertIn('class="day active" aria-pressed="true" aria-label="12 июля: 1 публичное событие"', html)
        self.assertIn('<div class="date-big" id="sheetDate">12</div>', html)

    def test_day_sheet_lists_only_published_events(self) -> None:
        html = render_page(make_dto())
        self.assertIn("Мастер-класс по записи", html)
        self.assertIn("1 ПУБЛИЧНОЕ СОБЫТИЕ", html)

    def test_log_lists_public_events_and_more_button_state(self) -> None:
        html = render_page(make_dto())
        self.assertIn("Открытый концерт", html)
        self.assertIn("<button class=\"more\" id=\"more\" type=\"button\" hidden>", html)

    def test_calendar_offset_matches_the_monday_first_grid(self) -> None:
        # 1 November 2026 is a Sunday: six leading empty cells, 42 cells total,
        # exactly like the approved prototype grid
        period = PeriodDTO(requested="2026-11", covered_start=date(2026, 11, 1),
                           covered_end=date(2026, 11, 30), complete=False)
        dto = EventsDTO(
            period=period, as_of=date(2026, 11, 27), release_id="x", freshness="fresh",
            verification="operational", completeness="complete", source_labels=("Teamup",),
            public_events=(PublicEventDTO(on=date(2026, 11, 5), type="open", title="Тест",
                                          description=None, announce_url=None,
                                          registration_open=False),),
            meetings=(), event_type_aggregates={},
            event_type_details=(EventTypeAggregateDTO(type="open", count=1, public=True),),
            calendar_days={"2026-11-05": 1}, meeting_days={}, calendar_granularity="day",
            metrics={"total_events": 2, "public_events": 1},
        )
        html = render_page(dto, period_label="2026-11")
        self.assertEqual(html.count('class="day empty"'), 12)  # 6 leading + 6 trailing
        self.assertIn('<span class="day empty" aria-hidden="true"></span>'
                      '<button type="button" class="day" aria-label="1 ноября: 0 публичных событий">', html)

    def test_no_inline_styles_or_scripts_anywhere(self) -> None:
        html = render_page(make_dto())
        self.assertNotIn('style="', html)
        self.assertNotIn("<script>", html)
        for chunk in html.split("<script")[1:]:
            self.assertTrue(chunk.startswith(' src="/static/') or chunk.startswith(' type="application/json"'))
        self.assertIn('href="/static/events.css"', html)

    def test_navigation_top_and_bottom_match_with_period(self) -> None:
        html = render_page(make_dto())
        navs = re.findall(r'<nav class="nav nav-(?:top|bottom)"[^>]*>(.*?)</nav>', html, re.S)
        self.assertEqual(len(navs), 2)
        self.assertEqual(navs[0], navs[1])
        self.assertIn('href="/events?period=2025-07" aria-current="page"', html)
        self.assertIn('href="/money?period=2025-07"', html)

    def test_period_options_use_month_names(self) -> None:
        html = render_page(make_dto())
        self.assertIn('<option value="2025-07" selected>Июль 2025</option>', html)
        self.assertIn('<option value="2025-06">Июнь 2025</option>', html)

    def test_logo_and_photo_are_page_assets_when_present(self) -> None:
        html = render_page(make_dto(), logo_url="/static/events-logo.webp",
                           photo_url="/static/events-basement.jpg")
        self.assertIn('src="/static/events-logo.webp"', html)
        self.assertIn('src="/static/events-basement.jpg"', html)

    def test_absent_photo_asset_leaves_no_broken_image(self) -> None:
        html = render_page(make_dto())
        self.assertNotIn("<img", html.split("<main")[1].split("</header>")[0])


class ServedRouteTests(unittest.TestCase):
    """The route itself serves the approved poster from a real release build."""

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.release_dir = base / "release"
        dev_release.build(str(cls.release_dir), period=PERIOD)
        cls.site = PublicSite(release_dir=cls.release_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def dispatch(self, path: str, query: str = f"period={PERIOD}"):
        saved: list = []
        body = b"".join(self.site({"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query},
                                  lambda s, h: saved.extend((s, h))))
        headers = dict(saved[1])
        status = int(str(saved[0]).split()[0])
        return status, headers, body.decode("utf-8", "replace")

    def test_events_route_serves_the_poster(self) -> None:
        status, headers, html = self.dispatch("/events")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("МЕРО", html)
        self.assertIn("/static/events.css", html)
        self.assertIn("/static/events.js", html)
        self.assertEqual(headers["Content-Security-Policy"], CSP)

    def test_served_page_shows_public_and_hides_closed(self) -> None:
        _status, _headers, html = self.dispatch("/events")
        self.assertIn("Открытый концерт", html)
        self.assertIn("Мастер-класс по записи", html)
        for closed in ("Закрытая встреча коммунаров", "Внутреннее собрание", "Репетиция"):
            self.assertNotIn(closed, html)

    def test_served_page_carries_no_demo_markers(self) -> None:
        _status, _headers, html = self.dispatch("/events")
        for marker in ("МАКЕТ", "makeEvents", "ДЕМО", "Эсперанто", "Кинопоказ и обсуждение",
                       "пример оформления", "О данных прототипа", "демонстрационный"):
            self.assertNotIn(marker, html)

    def test_period_switch_changes_the_poster(self) -> None:
        _status, _headers, html = self.dispatch("/events", "period=2025-07")
        self.assertIn("Июль 2025", html)
        other_status, _headers, other = self.dispatch("/events", "period=2025-07&granularity=month")
        self.assertEqual(other_status, 200)

    def test_unknown_period_parameter_is_refused(self) -> None:
        status, _headers, body = self.dispatch("/events", "period=all&secret=1")
        self.assertEqual(status, 400)
        self.assertNotIn("events-data", body)  # no poster data on a refused request

    def test_static_assets_are_served_with_right_types(self) -> None:
        for name, ctype in (
            ("/static/events.css", "text/css; charset=utf-8"),
            ("/static/events.js", "text/javascript; charset=utf-8"),
            ("/static/events-logo.webp", "image/webp"),
        ):
            status, headers, _body = self.dispatch(name, query="")
            with self.subTest(name=name):
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], ctype)


if __name__ == "__main__":
    unittest.main()
