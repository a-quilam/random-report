"""Focused checks for the collage poster of the home sheet (card t_7961a208).

The design transfer itself is reviewed in ``test_web_leaflet_composition``; this
module pins the *new* behaviour the transfer added, which had no test of its own:

* the home heading is one ``<h1>`` whose accessible name is the whole question,
  assembled from four paper scraps;
* navigation is seven real links with the leaflet's own addresses — not seven
  identical buttons;
* ``plural``/``month_in`` read Russian correctly for 1/2/5/11/21 and answer
  ``None`` where a month name makes no sense (a year, a range, «всё время»);
* ``subscriber_delta``/``_delta_line`` state a change whenever at least 8 days of
  real dated history exist (variant A, t_3da0472f), print the *real* interval —
  a base far outside the window must not be labelled «за 30 дней» (the defect
  review round 1 returned) — and say «истории не хватает» instead of inventing
  growth when the history is shorter than 8 days;
* ``announcements_block`` prints a card only for a record that carries a real
  number of forwards *and* a real link, largest first, at most three, and names
  the gap when the allowed data has no counters at all;
* the left column of the home sheet shows the approved basement photograph when
  the operator asset is installed in the served static contour (it is never in
  git), and keeps the honest frame as the fallback when it is absent.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from collections import namedtuple
from datetime import date
from pathlib import Path
from typing import Any

from randreport.web import render
from randreport.web.app import PublicSite
from tests import dev_release

PERIOD = "2025-07"

#: A dated subscribers snapshot as the DTO exposes it (``on`` + ``subscribers``).
Point = namedtuple("Point", "on subscribers")

NAV_CUT_RE = re.compile(
    r'<a class="nav-cut scrap (paper-[a-z]+)" href="([^"]+)"([^>]*)>([^<]*)</a>'
)


def points(*pairs: tuple[date, int]) -> list[Point]:
    return [Point(on, subscribers) for on, subscribers in pairs]


class TestTheHomeHeadline(unittest.TestCase):
    """The h1 is one heading from four scraps, not a picture to guess at."""

    def test_the_h1_names_the_whole_question_for_a_screen_reader(self) -> None:
        html = render.home_headline()
        self.assertEqual(1, len(re.findall(r"<h1[ >]", html)))
        self.assertIn('<h1 class="headline" aria-label="А дорог ли РанДом?">', html)

    def test_the_heading_is_four_paper_scraps_that_stay_decorative(self) -> None:
        html = render.home_headline()
        for scrap in ("head-a", "head-dorog", "head-random", "head-question"):
            with self.subTest(scrap=scrap):
                self.assertIn(scrap, html)
        # every visible fragment is decorative: the heading itself carries the text
        self.assertEqual(4, html.count('aria-hidden="true"'))
        self.assertIn('>А</span>', html)
        self.assertIn(">дорог ли</span>", html)
        self.assertIn(">РанДом</span>", html)
        self.assertIn(">?</span>", html)


class TestTheSevenNavigationCuts(unittest.TestCase):
    """Navigation is seven characteristic paper cuts with real addresses."""

    def test_exactly_seven_cuts_lead_to_the_leaflet_addresses(self) -> None:
        html = render.nav_cuts("/money", PERIOD)
        cuts = NAV_CUT_RE.findall(html)
        self.assertEqual(7, len(cuts))
        self.assertEqual(
            [href for href, _label, _paper in render.NAV_CUTS],
            [href.split("?")[0] for _paper, href, _attrs, _label in cuts],
        )
        for _paper, href, _attrs, label in cuts:
            with self.subTest(href=href):
                self.assertTrue(href.startswith("/"), f"{href!r} is not a same-origin path")
                self.assertTrue(href.endswith(f"?period={PERIOD}"), f"{href!r} drops the period")
                self.assertTrue(label.strip(), "a nav cut renders without a label")

    def test_the_cut_labels_are_the_documented_section_names(self) -> None:
        html = render.nav_cuts("/money", PERIOD)
        for _href, label, _paper in render.NAV_CUTS:
            with self.subTest(label=label):
                self.assertIn(f">{label}</a>", html)

    def test_only_the_open_section_is_marked_as_current(self) -> None:
        html = render.nav_cuts("/money", PERIOD)
        current = [
            href for _paper, href, attrs, _label in NAV_CUT_RE.findall(html) if 'aria-current="page"' in attrs
        ]
        self.assertEqual([f"/money?period={PERIOD}"], current)

    def test_every_served_home_page_carries_the_same_seven_cuts(self) -> None:
        with _served_home() as html:
            cuts = NAV_CUT_RE.findall(html)
            self.assertEqual(1, len(re.findall(r"<h1[ >]", html)))
            self.assertIn('<h1 class="headline" aria-label="А дорог ли РанДом?">', html)
            self.assertEqual(7, len(cuts))
            self.assertEqual(
                [href for href, _label, _paper in render.NAV_CUTS],
                [href.split("?")[0] for _paper, href, _attrs, _label in cuts],
            )


class TestNumberWords(unittest.TestCase):
    """The poster spells its counts in Russian; a wrong form reads as a mistake."""

    def test_plural_one_two_five_eleven_twenty_one(self) -> None:
        word = lambda value: render.plural(value, "событие", "события", "событий")  # noqa: E731
        for value, expected in ((1, "событие"), (2, "события"), (5, "событий"), (11, "событий"), (21, "событие")):
            with self.subTest(value=value):
                self.assertEqual(expected, word(value))
        # the tens that repeat the teens rule
        self.assertEqual("событий", word(111))
        self.assertEqual("событие", word(101))

    def test_plural_without_a_number_stays_the_neutral_form(self) -> None:
        self.assertEqual("подписчиков", render.plural(None, "подписчик", "подписчика", "подписчиков"))

    def test_month_in_names_only_a_real_month_period(self) -> None:
        self.assertEqual("июле", render.month_in("2025-07"))
        self.assertEqual("ноябре", render.month_in("2026-11"))
        for label in ("2025", "all", "2025-04..2025-08", "2025-13", ""):
            with self.subTest(label=label):
                self.assertIsNone(render.month_in(label))


class TestSubscriberDelta(unittest.TestCase):
    """A change is stated only for a full 30-day window, and the interval is real."""

    def delta_and_line(self, history: list[Point]) -> tuple[int | None, str | None, str]:
        delta, base_on, span_days = render.subscriber_delta(history)
        return delta, base_on, render._delta_line(delta, base_on, span_days=span_days)

    def test_one_dated_point_is_no_history(self) -> None:
        delta, base_on, line = self.delta_and_line(points((date(2025, 7, 1), 4128)))
        self.assertIsNone(delta)
        self.assertIsNone(base_on)
        self.assertEqual("прироста за 30 суток нет: истории не хватает", line)

    def test_no_points_at_all_is_no_history(self) -> None:
        self.assertEqual((None, None, "прироста за 30 суток нет: истории не хватает"), self.delta_and_line([]))

    def test_twenty_days_of_history_state_their_real_interval(self) -> None:
        # Variant A (t_3da0472f): ≥8 days of real history now answer with the
        # actual interval length instead of the honest refusal.
        delta, base_on, line = self.delta_and_line(
            points((date(2025, 7, 10), 4100), (date(2025, 7, 30), 4224))
        )
        self.assertEqual(124, delta)
        self.assertEqual("2025-07-10", base_on)
        self.assertEqual("↗ +124 за 20 дней (с 2025-07-10)", line)

    def test_seven_days_of_history_is_still_no_history(self) -> None:
        delta, base_on, line = self.delta_and_line(
            points((date(2025, 7, 23), 4100), (date(2025, 7, 30), 4224))
        )
        self.assertIsNone(delta)
        self.assertIsNone(base_on)
        self.assertIn("истории не хватает", line)

    def test_a_base_exactly_thirty_days_back_counts(self) -> None:
        delta, base_on, line = self.delta_and_line(
            points((date(2025, 6, 30), 4100), (date(2025, 7, 30), 4224))
        )
        self.assertEqual(124, delta)
        self.assertEqual("2025-06-30", base_on)
        self.assertEqual("↗ +124 за 30 дней (с 2025-06-30)", line)

    def test_a_rolled_over_interval_states_its_real_length(self) -> None:
        # Regression of review round 1: with points only in April and July the
        # change covers 120 days, so «за 30 дней» was a false label.
        delta, base_on, line = self.delta_and_line(
            points((date(2025, 4, 1), 4000), (date(2025, 7, 30), 4224))
        )
        self.assertEqual(224, delta)
        self.assertEqual("2025-04-01", base_on)
        self.assertEqual("↗ +224 за 120 дней (с 2025-04-01)", line)
        self.assertNotIn("за 30 дней", line)

    def test_the_nearest_base_at_or_before_the_cutoff_is_the_one_used(self) -> None:
        delta, base_on, _line = self.delta_and_line(
            points(
                (date(2025, 4, 1), 4000),
                (date(2025, 6, 29), 4100),
                (date(2025, 6, 30), 4104),
                (date(2025, 7, 30), 4224),
            )
        )
        self.assertEqual(120, delta)
        self.assertEqual("2025-06-30", base_on)

    def test_a_decline_is_printed_with_its_own_arrow_and_sign(self) -> None:
        _delta, _base, line = self.delta_and_line(
            points((date(2025, 6, 30), 4200), (date(2025, 7, 30), 4150))
        )
        self.assertEqual("↘ −50 за 30 дней (с 2025-06-30)", line)

    def test_the_poster_prints_that_line_under_the_reached_figure(self) -> None:
        html = render._poster_figures(
            period_label=PERIOD,
            events_current=3,
            events_previous=2,
            subscribers=4224,
            subscribers_on="2025-07-30",
            delta=224,
            delta_from="2025-04-01",
            delta_span_days=121,
        )
        self.assertIn("↗ +224 за 121 день", html)
        self.assertIn("с 2025-04-01", html)


class TestAnnouncementsBlock(unittest.TestCase):
    """«Что пересылали» shows a real ranking or nothing at all."""

    def test_a_card_needs_both_a_real_counter_and_a_real_link(self) -> None:
        html = render.announcements_block(
            [
                {"title": "Есть счётчик", "forwards": 12, "url": "https://t.me/random_culture/700"},
                {"title": "Без счётчика", "forwards": None, "url": "https://t.me/random_culture/701"},
                {"title": "Без ссылки", "forwards": 5, "url": ""},
            ]
        )
        self.assertEqual(1, html.count('class="forward"'))
        self.assertIn("Есть счётчик", html)
        self.assertNotIn("Без счётчика", html)
        self.assertNotIn("Без ссылки", html)
        self.assertIn('href="https://t.me/random_culture/700"', html)
        self.assertIn('rel="noopener"', html)

    def test_at_most_three_cards_largest_first(self) -> None:
        items = [
            {"title": f"пост {value}", "forwards": value, "url": f"https://t.me/random_culture/{value}"}
            for value in (1, 5, 3, 4, 2)
        ]
        html = render.announcements_block(items)
        self.assertEqual(3, html.count('class="forward"'))
        for value in (5, 4, 3):
            with self.subTest(kept=value):
                self.assertIn(f"пост {value}", html)
        self.assertNotIn("пост 2<", html)
        self.assertNotIn("пост 1<", html)
        # the order is the ranking, not the input order
        self.assertLess(html.index("пост 5"), html.index("пост 4"))
        self.assertLess(html.index("пост 4"), html.index("пост 3"))
        self.assertEqual(["№ 01", "№ 02", "№ 03"], re.findall(r'<span class="rank">([^<]+)</span>', html))

    def test_a_zero_counter_is_a_real_counter(self) -> None:
        # a confirmed zero forwards is data, not a gap: the card still prints
        html = render.announcements_block(
            [{"title": "Ноль пересылок", "forwards": 0, "url": "https://t.me/random_culture/702"}]
        )
        self.assertEqual(1, html.count('class="forward"'))
        self.assertIn("↗ 0 пересылок", html)

    def test_no_records_names_the_gap_instead_of_a_ranking(self) -> None:
        for items in ((), [{"title": "Без счётчика", "forwards": None, "url": "https://t.me/x/1"}]):
            with self.subTest(items=items):
                html = render.announcements_block(items)
                self.assertIn("Счётчиков пересылок в публичном выпуске пока нет", html)
                self.assertNotIn('class="forward"', html)
                self.assertNotIn("№ 0", html)

    def test_an_unsafe_url_from_a_source_document_never_becomes_a_card(self) -> None:
        # an empty safe_url means there is no link, so there is no card
        html = render.announcements_block(
            [{"title": "Скрипт", "forwards": 9, "url": "javascript:alert(1)"}]
        )
        self.assertNotIn("javascript:", html)
        self.assertNotIn('class="forward"', html)
        self.assertIn("Счётчиков пересылок в публичном выпуске пока нет", html)


class _served_home:
    """The home page through the real WSGI callable, on a synthetic release."""

    def __init__(self, period: str = PERIOD) -> None:
        self._period = period

    def __enter__(self) -> str:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        dev_release.build(str(root / "release"), period=PERIOD, db_path=str(root / "build.sqlite"))
        site = PublicSite(release_dir=root / "release")
        try:
            return site._dispatch(
                {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": f"period={self._period}"}
            ).text
        finally:
            site.close()

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


class TestTheHonestEmptyFigures(unittest.TestCase):
    """A missing figure is a word, so it must stay inside its own cut.

    Measured in a real browser: at the poster's number size «нет данных» was wider
    than its column and ran off the sheet at 2048/1440/768/390/320 on the period a
    visitor lands on by default (a month with no release yet). The honest word now
    prints on the same paper in a smaller cut.
    """

    def poster(self, **values: Any) -> str:
        fields = {
            "period_label": PERIOD,
            "events_current": None,
            "events_previous": None,
            "subscribers": None,
            "subscribers_on": None,
            "delta": None,
            "delta_from": None,
        }
        fields.update(values)
        return render._poster_figures(**fields)

    def test_a_missing_value_prints_the_word_on_a_smaller_cut(self) -> None:
        html = self.poster()
        self.assertEqual(2, html.count("нет данных"))
        self.assertIn('class="scrap paper-purple number wide"', html)
        self.assertIn('class="scrap paper-yellow number wide"', html)

    def test_a_real_figure_keeps_the_poster_size(self) -> None:
        html = self.poster(events_current=6, subscribers=4128)
        self.assertIn('class="scrap paper-purple number">6</span>', html)
        self.assertIn('class="scrap paper-yellow number">4 128</span>', html)
        self.assertNotIn("number wide", html)

    def test_only_the_missing_figure_shrinks_when_one_value_exists(self) -> None:
        html = self.poster(events_current=6, subscribers=None)
        self.assertNotIn('paper-purple number wide', html)
        self.assertIn('class="scrap paper-yellow number wide"', html)

    def test_the_smaller_cut_is_pinned_in_the_stylesheet(self) -> None:
        css = (Path(__file__).resolve().parents[1] / "randreport" / "web" / "static" / "site.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".metric .number.wide {", css)
        self.assertIn(".metric .number.wide", css[css.index("@media (max-width: 800px)") :])

    def test_the_served_home_of_a_period_without_a_release_uses_it(self) -> None:
        # 2026-01 has no release in the synthetic archive: the honest state a
        # visitor lands on when the current month is not published yet
        with _served_home("2026-01") as html:
            self.assertIn('class="scrap paper-purple number wide"', html)
            self.assertIn('class="scrap paper-yellow number wide"', html)
            self.assertIn("нет данных", html)


class TestTheApprovedPhotoOnTheHomeSheet(unittest.TestCase):
    """The left column carries the approved photo when the operator asset exists."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.release_dir = root / "release"
        dev_release.build(str(cls.release_dir), period=PERIOD, db_path=str(root / "build.sqlite"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def home(self, static_dir: Path | None) -> str:
        site = PublicSite(release_dir=self.release_dir, static_dir=static_dir)
        try:
            return site._dispatch(
                {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": f"period={PERIOD}"}
            ).text
        finally:
            site.close()

    def test_the_installed_asset_becomes_the_photo_of_the_left_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            static_dir = Path(tmp)
            (static_dir / "basement-photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            html = self.home(static_dir)
        self.assertIn('<figure class="visual has-photo"', html)
        self.assertIn('<img class="visual-cut photo" src="/static/basement-photo.png"', html)
        self.assertIn("alt=", html)
        # the frame and its caption are gone when the photo is there
        self.assertNotIn("появится здесь", html)
        self.assertNotIn("photo-placeholder", html)

    def test_a_lighter_jpeg_variant_wins_when_both_are_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            static_dir = Path(tmp)
            (static_dir / "basement-photo.jpg").write_bytes(b"\xff\xd8\xff")
            (static_dir / "basement-photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            html = self.home(static_dir)
        self.assertIn('src="/static/basement-photo.jpg"', html)
        self.assertNotIn("basement-photo.png", html)

    def test_without_the_asset_the_honest_frame_remains_the_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            html = self.home(Path(tmp))
        self.assertIn('<figure class="visual"', html)
        self.assertNotIn("has-photo", html)
        self.assertNotIn("<img", html)
        self.assertIn("когда будет разрешённый к публикации снимок", html)

    def test_the_mock_placeholder_is_never_published_in_either_state(self) -> None:
        for installed in (True, False):
            with self.subTest(photo=installed), tempfile.TemporaryDirectory() as tmp:
                static_dir = Path(tmp)
                if installed:
                    (static_dir / "basement-photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                html = self.home(static_dir)
                self.assertNotIn("photo-placeholder", html)


if __name__ == "__main__":
    unittest.main()
