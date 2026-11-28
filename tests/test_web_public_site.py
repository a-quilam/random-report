"""Acceptance tests for the public SSR site, its API, CSV and report archive.

Covers the matrix of this card: R01 (archive snapshot + correction with diff and a
new version), R02 (CSV from the allowlist only), U01 (desktop/mobile, keyboard,
empty/incomplete/stale states, period switching, public part without login) and
E03 (closed events aggregate-only, never a title, date, time, id or grid point),
plus the period allowlist and a real HTTP round-trip over a live server.

All data is synthetic: the release is built by ``tests.dev_release`` from offline
fake transports, and the historical plane is the transcribed public reports.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from wsgiref.simple_server import make_server

from randreport.domain.timeframes import month_period, range_period, year_period
from randreport.projection.csv_export import CSV_HEADER
from randreport.store import apply_migrations, connect
from randreport.store import repository as repo
from randreport.web import reports as reports_module
from randreport.web.app import PublicSite, parse_report_label
from randreport.web.release_index import ReleaseReadError
from randreport import history
from tests import dev_release

PERIOD = "2025-07"

HTML_ROUTES = ("/", "/money", "/events", "/community", "/support", "/chronicle", "/reports", "/methodology")

CLOSED_SECRETS = (
    "Закрытая встреча коммунаров",
    "Внутреннее собрание",
    "Репетиция",
    "Лекция",
    "2025-07-15",
    "2025-07-16",
    "2025-07-19",
    "11:15",
    "13:45",
    "ev_closed",
    "ev_internal",
    "ev_prep",
    "ev_unknown",
    "acc_personal",
    "acc_mixed",
    "cat_rent",
    "cat_brand_new",
    "sub_closed",
    "sub_internal",
)


def make_wsgi_request(site: PublicSite, path: str, query: str = "") -> tuple[int, str]:
    """One request through the real WSGI callable; returns (status, body)."""

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> None:
        captured["status"] = status

    body = b"".join(site(environ, start_response)).decode("utf-8")
    return int(str(captured["status"]).split()[0]), body


class Fixture:
    """One synthetic release on disk plus a seeded report-archive database."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.release_dir = tmp / "release"
        self.report_db = tmp / "reports.sqlite"
        self.build_db = tmp / "build.sqlite"

        dev_release.build(str(self.release_dir), period=PERIOD, db_path=str(self.build_db))

        conn = connect(str(self.report_db))
        apply_migrations(conn)
        reports_module.seed_published_snapshots(conn, published_at="2026-01-10T00:00:00+00:00")
        conn.close()

        self.site = PublicSite(release_dir=self.release_dir, db_path=self.report_db)

    def get(self, path: str, query: str = "") -> tuple[int, str, dict[str, str]]:
        """Issue a request through the real WSGI interface (status + headers + body)."""

        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> None:
            captured["status"] = status
            captured["headers"] = headers

        body = b"".join(self.site(environ, start_response)).decode("utf-8")
        headers_map = {k.lower(): v for k, v in captured["headers"]}
        return int(str(captured["status"]).split()[0]), body, headers_map

    def plain(self, path: str, query: str = "") -> str:
        _status, text, _headers = self.get(path, query)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))


class SiteTestCase(unittest.TestCase):
    tmpdir: tempfile.TemporaryDirectory
    fixture: Fixture

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.fixture = Fixture(Path(cls.tmpdir.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.site.close()
        cls.tmpdir.cleanup()

    def setUp(self) -> None:
        self.site = self.fixture.site


# --------------------------------------------------------------------------- #
# U01 - the eight sections, states, period switching, no login
# --------------------------------------------------------------------------- #

class TestEightSections(SiteTestCase):
    def test_every_section_renders_without_login(self) -> None:
        for route in HTML_ROUTES:
            with self.subTest(route=route):
                status, text, _headers = self.fixture.get(route, f"period={PERIOD}")
                self.assertEqual(status, 200)
                if route == "/events":
                    # the owner-approved events poster keeps its own landmarks:
                    # `#top` main and the prototype's visually-hidden skip link
                    self.assertIn('<main class="page" id="top">', text)
                    self.assertIn('<a class="sr" href="#calendar">', text)
                elif route == "/money":
                    self.assertIn('<main id="main"', text)
                    self.assertIn('<a class="skip" href="#main">', text)
                else:
                    self.assertIn('<main id="content"', text)
                    self.assertIn('<a class="skip" href="#content">', text)
                self.assertIn('lang="ru"', text)
                self.assertIn('name="viewport"', text)
                self.assertNotIn("password", text.lower())

    def test_every_section_renders_for_historical_and_empty_periods(self) -> None:
        for period in ("2025", "2025-01", "2024-03", "all"):
            for route in HTML_ROUTES:
                with self.subTest(period=period, route=route):
                    status, _text, _headers = self.fixture.get(route, f"period={period}")
                    self.assertEqual(status, 200)

    def test_period_is_preserved_in_navigation_and_the_switcher(self) -> None:
        status, text, _headers = self.fixture.get("/money", f"period={PERIOD}")
        self.assertEqual(status, 200)
        # the approved sheet keeps plain "/" for the home slip; the other slips
        # and the switcher still carry the selected period
        for href, _label in (("/money", "Деньги"), ("/events", "Мероприятия"), ("/reports", "Архив")):
            self.assertIn(f'href="{href}?period={PERIOD}"', text)
        self.assertRegex(text, r'<select id="period-select" name="period"( aria-label="[^"]*")?>')
        self.assertIn(f'<option value="{PERIOD}" selected>', text)
        self.assertIn('<option value="all">Всё время</option>', text)
        self.assertIn('<label for="period-select">', text)

    def test_keyboard_and_screen_reader_markup(self) -> None:
        status, text, _headers = self.fixture.get("/money", f"period={PERIOD}")
        self.assertEqual(status, 200)
        self.assertIn("<caption>", text)  # every table is captioned
        self.assertIn("текстовая версия графика", text)  # charts have a table alternative
        events_status, events_text, _h = self.fixture.get("/events", f"period={PERIOD}")
        self.assertEqual(events_status, 200)
        self.assertIn('aria-current="page"', events_text)
        # the poster has no tables; its keyboard story is the mock's own markup:
        # selectable day buttons, a labelled month select, hidden weekday row
        self.assertIn('aria-label="Выбрать месяц"', events_text)
        self.assertIn('<div class="weekday-grid" aria-hidden="true">', events_text)
        self.assertIn('aria-live="polite"', events_text)
        self.assertIn('class="sr"', events_text)  # visually hidden skip link
        self.assertIn("dialog", events_text)

    def test_empty_and_unknown_states_are_explicit(self) -> None:
        text = self.fixture.plain("/money", "period=2024-03")
        self.assertIn("нет данных", text)
        self.assertIn("Опубликованных исторических агрегатов за этот период нет", text)

    def test_incomplete_and_operational_states_are_labelled(self) -> None:
        text = self.fixture.plain("/money", f"period={PERIOD}")
        self.assertIn("период неполон", text)
        self.assertIn("оперативные, без ручной проверки", text)
        # the unknown facts stay unknown: the balance is not printed at all,
        # and the sheet is stamped as preliminary instead of faking totals
        self.assertIn("ПРЕДВАРИТЕЛЬНО", text)
        self.assertIn("не итог периода", text)
        self.assertNotIn("ОСТАТОК В КАССЕ", text)

    def test_stale_or_missing_freshness_is_labelled(self) -> None:
        text = self.fixture.plain("/community", "period=2024-03")
        self.assertIn("нет данных", text)
        self.assertIn("Свежесть", text)

    def test_statuses_are_not_colour_only(self) -> None:
        status, text, _headers = self.fixture.get("/money", f"period={PERIOD}")
        self.assertEqual(status, 200)
        for label in ("Свежесть", "Проверка", "Полнота", "Период", "Источники"):
            self.assertIn(label, text)

    def test_mobile_layout_is_provided(self) -> None:
        status, css, _headers = self.fixture.get("/static/site.css")
        self.assertEqual(status, 200)
        self.assertIn("@media (max-width: 480px)", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("overflow-x: auto", css)  # tables scroll without breaking the page


# --------------------------------------------------------------------------- #
# E03 - closed data never reaches the public surface
# --------------------------------------------------------------------------- #

class TestClosedDataNeverLeaks(SiteTestCase):
    def test_html_pages_never_contain_closed_details(self) -> None:
        for period in ("2025-07", "2025", "all"):
            for route in HTML_ROUTES:
                with self.subTest(period=period, route=route):
                    _status, text, _headers = self.fixture.get(route, f"period={period}")
                    for secret in CLOSED_SECRETS:
                        self.assertNotIn(secret, text, f"{route}?period={period} leaked {secret!r}")

    def test_api_and_csv_never_contain_closed_details(self) -> None:
        for endpoint in (
            "/api/public/overview",
            "/api/public/money",
            "/api/public/events",
            "/api/public/community",
            "/api/public/chronicle",
            "/api/public/export/categories.csv",
        ):
            for period in ("2025-07", "2025"):
                with self.subTest(endpoint=endpoint, period=period):
                    _status, text, _headers = self.fixture.get(endpoint, f"period={period}")
                    for secret in CLOSED_SECRETS:
                        self.assertNotIn(secret, text)

    def test_closed_events_are_counted_but_not_listed(self) -> None:
        _status, text, _headers = self.fixture.get("/api/public/events", f"period={PERIOD}")
        payload = json.loads(text)
        self.assertEqual(payload["event_type_aggregates"]["closed"], 1)
        self.assertEqual(payload["event_type_aggregates"]["internal_meeting"], 1)
        self.assertEqual(payload["event_type_aggregates"]["preparation"], 1)
        self.assertEqual(payload["event_type_aggregates"]["unknown"], 1)
        self.assertEqual(len(payload["public_events"]), 2)
        self.assertTrue(all(item["type"] in ("open", "open_by_registration") for item in payload["public_events"]))

    def test_calendar_grid_has_no_closed_day(self) -> None:
        _status, text, _headers = self.fixture.get("/api/public/events", f"period={PERIOD}")
        payload = json.loads(text)
        self.assertEqual(payload["calendar_granularity"], "day")
        self.assertEqual(set(payload["calendar_days"]), {"2025-07-05", "2025-07-12"})
        self.assertNotIn("2025-07-15", payload["calendar_days"])

    def test_static_export_has_no_closed_details(self) -> None:
        out = Path(self.tmpdir.name) / "static"
        self.site.export_static(out, periods=[PERIOD, "2025"])
        files = list(out.rglob("*.html"))
        self.assertTrue(files)
        for file in files:
            text = file.read_text(encoding="utf-8")
            for secret in CLOSED_SECRETS:
                self.assertNotIn(secret, text, f"{file} leaked {secret!r}")

    def test_report_page_never_mentions_a_closed_event(self) -> None:
        for label in ("2025-01", "2025", "2025-04..2025-08", "2025-07"):
            _status, text, _headers = self.fixture.get(f"/reports/{label}")
            for secret in CLOSED_SECRETS:
                self.assertNotIn(secret, text)


# --------------------------------------------------------------------------- #
# R02 - the CSV is exactly the allowlist
# --------------------------------------------------------------------------- #

class TestCsvAllowlist(SiteTestCase):
    def test_csv_header_is_the_frozen_allowlist(self) -> None:
        status, text, headers = self.fixture.get("/api/public/export/categories.csv", f"period={PERIOD}")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"])
        self.assertEqual(text.splitlines()[0], ",".join(CSV_HEADER))

    def test_csv_rows_carry_only_public_aggregates(self) -> None:
        _status, text, _headers = self.fixture.get("/api/public/export/categories.csv", f"period={PERIOD}")
        rows = [line.split(",") for line in text.splitlines()[1:]]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row[0], PERIOD)
            self.assertTrue(row[1].startswith("k"))
            self.assertEqual(row[8], "RUB_kopecks")
        for forbidden in ("transaction_id", "account_id", "counterparty", "occurrence_id", "subcalendar"):
            self.assertNotIn(forbidden, text)

    def test_non_standard_parameters_are_refused(self) -> None:
        for query in (
            f"period={PERIOD}&account_id=acc_card",
            f"period={PERIOD}&transaction_id=tx_rent",
            f"period={PERIOD}&day=2025-07-05",
            f"period={PERIOD}&granularity=day",
            f"period={PERIOD}&unit=USD",
        ):
            with self.subTest(query=query):
                status, _text, _headers = self.fixture.get("/api/public/export/categories.csv", query)
                self.assertEqual(status, 400)

    def test_day_level_period_is_refused_everywhere(self) -> None:
        for path in ("/money", "/api/public/money", "/api/public/export/categories.csv", "/api/public/overview"):
            with self.subTest(path=path):
                status, _text, _headers = self.fixture.get(path, "period=2025-07-05")
                self.assertEqual(status, 400)

    def test_csv_is_refused_for_an_unpublished_period(self) -> None:
        status, text, _headers = self.fixture.get("/api/public/export/categories.csv", "period=2025-01")
        self.assertEqual(status, 404)
        for secret in CLOSED_SECRETS:
            self.assertNotIn(secret, text)

    def test_csv_is_refused_for_the_whole_archive(self) -> None:
        status, _text, _headers = self.fixture.get("/api/public/export/categories.csv", "period=all")
        self.assertEqual(status, 400)


# --------------------------------------------------------------------------- #
# R01 - the version archive
# --------------------------------------------------------------------------- #

class TestCoverageBar(SiteTestCase):
    """Section 3.1: a 100 % coverage bar whose printed numbers agree with its table."""

    def _widths(self, html: str) -> list[Any]:
        from decimal import Decimal as D

        # the segment width is SVG geometry, never an inline style declaration
        # (the site's CSP is `style-src 'self'`, which drops a style attribute)
        self.assertNotIn('style="width:', html)
        return [D(value) for value in re.findall(r'<rect class="seg[^"]*" x="[0-9.]+" y="0" width="([0-9.]+)"', html)]

    def test_segments_never_overrun_the_bar_and_match_the_table(self) -> None:
        from decimal import Decimal as D

        from randreport.web import render

        html = render.coverage_bar(
            [("донаты", D("18.4")), ("ивенты", D("9.2")), ("склад", D("2.3"))],
            total_percent=D("30.0"),
        )
        widths = self._widths(html)
        self.assertLessEqual(sum(widths), D("100"))
        self.assertEqual(sum(widths), D("100"))
        self.assertIn("недостающая часть 70,0 %", html)
        self.assertIn("70,0 %", html)
        self.assertNotIn("width:-", html)

    def test_more_than_hundred_percent_does_not_draw_a_negative_segment(self) -> None:
        from decimal import Decimal as D

        from randreport.web import render

        html = render.coverage_bar([("компенсация", D("110.0"))], total_percent=D("110.0"))
        widths = self._widths(html)
        self.assertEqual(sum(widths), D("100"))
        self.assertNotIn("недостающая часть", html)
        self.assertIn("110,0 %", html)

    def test_unknown_coverage_draws_no_percentage(self) -> None:
        from randreport.web import render

        html = render.coverage_bar([("компенсация", None)], total_percent=None)
        self.assertIn("не применимо", html)
        self.assertIn("нет данных", html)

    def test_money_page_bar_segments_come_from_the_category_rows(self) -> None:
        status, text, _headers = self.fixture.get("/money", f"period={PERIOD}")
        self.assertEqual(status, 200)
        for label in ("Донаты", "Доля с ивента", "Склад"):
            self.assertIn(f"{label} ", text)
        self.assertIn("недостающая часть", text)


class TestReportArchive(SiteTestCase):
    def test_seeded_published_snapshots_are_version_one(self) -> None:
        conn = connect(str(self.fixture.report_db))
        try:
            rows = reports_module.versions(conn, "2025-01")
        finally:
            conn.close()
        self.assertEqual(rows[0]["version"], 1)
        self.assertEqual(rows[0]["original_url"], "https://t.me/random_culture/587")

    def test_correction_adds_a_version_and_keeps_the_original(self) -> None:
        conn = connect(str(self.fixture.report_db))
        try:
            before = reports_module.version_payload(conn, "2025-01", 1)
            result = reports_module.record_correction(
                conn,
                period_label="2025-01",
                changes={"expenses": 123456},
                reason="уточнена сумма аренды за январь",
                actor="editor-1",
                published_at="2026-02-01T00:00:00+00:00",
            )
            after = reports_module.version_payload(conn, "2025-01", 1)
            latest = reports_module.version_payload(conn, "2025-01", 2)
        finally:
            conn.close()
        self.assertEqual(result["version"], 2)
        self.assertEqual(before, after)  # the original publication is never rewritten
        self.assertEqual(latest["provenance"], "corrected")
        self.assertEqual(latest["expenses"], 123456)
        self.assertEqual(latest["corrections"][0]["reason"], "уточнена сумма аренды за январь")
        self.assertEqual(latest["corrections"][0]["changes"]["expenses"], {"from": None, "to": 123456})

    def test_a_version_cannot_be_rewritten(self) -> None:
        conn = connect(str(self.fixture.report_db))
        try:
            with self.assertRaises(ValueError):
                repo.record_report_snapshot(
                    conn,
                    report_id="report:2025-01",
                    period_label="2025-01",
                    version=1,
                    published_at="2026-01-10T00:00:00+00:00",
                    payload={"period": "2025-01", "events": 999},
                    original_text=None,
                    original_url="https://t.me/random_culture/587",
                )
        finally:
            conn.close()

    def test_report_text_uses_only_snapshot_facts(self) -> None:
        report = history.exact_report(year_period("2025"))
        assert report is not None
        payload = reports_module.snapshot_payload(report)
        text = reports_module.report_text(payload)
        reports_module.assert_text_faithful(text, payload)
        self.assertIn("707 920", text)
        self.assertIn("1 267 226", text)
        self.assertIn("покрытие 55,9 %", text)
        # a number the snapshot does not contain is rejected
        with self.assertRaises(reports_module.ReportError):
            reports_module.assert_text_faithful(text + "\nИтого собрано 987654 ₽", payload)

    def test_public_report_page_shows_a_correction_with_a_permanent_url(self) -> None:
        status, text, _headers = self.fixture.get("/reports/2025-01")
        self.assertEqual(status, 200)
        self.assertIn("Корректировки", text)
        self.assertIn("уточнена сумма аренды за январь", text)
        self.assertIn("/reports/2025-01/v1", text)
        self.assertIn("Исходная публикация не перезаписывается", text)

    def test_original_version_is_reachable_at_its_permanent_url(self) -> None:
        status, text, _headers = self.fixture.get("/reports/2025-01/v1")
        self.assertEqual(status, 200)
        self.assertIn("Отчёт за 2025-01", text)
        self.assertNotIn("123 456 ₽", text)  # v1 does not carry the later correction

    def test_report_page_of_a_covered_month_names_the_group_report(self) -> None:
        status, text, _headers = self.fixture.get("/reports/2025-06")
        self.assertEqual(status, 200)
        self.assertIn("отдельного отчёта нет", text)
        self.assertIn("2025-04..2025-08", text)

    def test_report_pages_are_reachable_for_every_published_period(self) -> None:
        for label in ("2025-01", "2025-02", "2025-03", "2025", "2025-04..2025-08"):
            with self.subTest(label=label):
                status, _text, _headers = self.fixture.get(f"/reports/{label}")
                self.assertEqual(status, 200)

    def test_parse_report_label_accepts_year_month_and_range(self) -> None:
        self.assertEqual(parse_report_label("2025").label, "2025")
        self.assertEqual(parse_report_label("2025-03").label, "2025-03")
        self.assertEqual(parse_report_label("2025-04..2025-08").label, "2025-04..2025-08")


# --------------------------------------------------------------------------- #
# Historical plane
# --------------------------------------------------------------------------- #

class TestHistoricalPlane(unittest.TestCase):
    def test_annual_aggregate_is_the_published_one(self) -> None:
        events, receipts, expenses = history.annual_view(2025)
        self.assertEqual(events.value, 459)
        self.assertEqual(receipts.value, 707920)
        self.assertEqual(expenses.value, 1267226)
        self.assertEqual(events.coverage, history.COMPLETE)

    def test_month_inside_a_group_report_is_partial_not_split(self) -> None:
        view = history.metric_view(month_period("2025-07"), "events")
        self.assertIsNone(view.value)
        self.assertEqual(view.coverage, history.PARTIAL)
        self.assertEqual(view.source.label, "2025-04..2025-08")

    def test_a_month_without_any_observation_is_unknown(self) -> None:
        view = history.metric_view(month_period("2024-01"), "events")
        self.assertIsNone(view.value)
        self.assertEqual(view.coverage, history.UNKNOWN)
        self.assertIsNone(view.source)

    def test_year_is_never_the_sum_of_months_with_a_gap(self) -> None:
        # a partial span (February..November) must not be summed silently
        view = history.metric_view(range_period("2025-02", "2025-11"), "events")
        self.assertEqual(view.coverage, history.PARTIAL)
        self.assertIsNone(view.value)

    def test_historical_balance_is_never_fabricated(self) -> None:
        self.assertIsNone(history.coverage_percent(707920, 0))
        self.assertEqual(str(history.coverage_percent(707920, 1267226)), "55.9")
        report = history.exact_report(year_period("2025"))
        assert report is not None
        text = reports_module.report_text(reports_module.snapshot_payload(report))
        self.assertIn("не остаток", text)

    def test_no_prototype_numbers_are_published(self) -> None:
        values = {obs.value for obs in history.observations() if obs.metric == "events"}
        self.assertEqual(values, {40, 52, 59, 147, 459})


# --------------------------------------------------------------------------- #
# Release projection reading
# --------------------------------------------------------------------------- #

class TestReleaseIndex(SiteTestCase):
    def test_release_round_trips_through_the_allowlist(self) -> None:
        release = self.site.index.current()
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.period_label, PERIOD)
        self.assertIsNotNone(release.money)

    def test_a_release_with_an_extra_field_cannot_be_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "rel_extra"
            target.mkdir()
            (target / "index.json").write_text(json.dumps({"release_id": "rel_extra", "as_of": "2025-07-31"}), encoding="utf-8")
            (target / "money.json").write_text(
                json.dumps(
                    {
                        "period": {"requested": PERIOD, "covered_start": "2025-07-01", "covered_end": "2025-07-31", "complete": True},
                        "as_of": "2025-07-31",
                        "release_id": "rel_extra",
                        "freshness": "fresh",
                        "verification": "operational",
                        "completeness": "complete",
                        "source_labels": [],
                        "unit": "RUB_kopecks",
                        "metrics": {},
                        "categories": [],
                        "account_id": "acc_card",
                    }
                ),
                encoding="utf-8",
            )
            site = PublicSite(release_dir=tmp)
            with self.assertRaises(ReleaseReadError):
                site.index.get("rel_extra")
            status, text = make_wsgi_request(site, "/money")
            self.assertEqual(status, 500)
            self.assertNotIn("acc_card", text)

    def test_site_without_a_release_directory_serves_empty_states(self) -> None:
        site = PublicSite(release_dir=None, db_path=None)
        for route in HTML_ROUTES:
            with self.subTest(route=route):
                response = site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": ""})
                self.assertEqual(response.status, 200)
                self.assertIn("нет данных", response.text.lower())


# --------------------------------------------------------------------------- #
# HTTP, headers and methods
# --------------------------------------------------------------------------- #

class TestRealHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.fixture = Fixture(Path(cls.tmpdir.name))
        cls.server = make_server("127.0.0.1", 0, cls.fixture.site)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.fixture.site.close()
        cls.tmpdir.cleanup()

    def request(self, method: str, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()

    def test_pages_and_api_answer_over_real_http(self) -> None:
        for path in (
            "/",
            f"/money?period={PERIOD}",
            f"/events?period={PERIOD}",
            "/reports/2025-01",
            "/reports",
            "/methodology",
            f"/api/public/overview?period={PERIOD}",
            f"/api/public/money?period={PERIOD}",
            f"/api/public/events?period={PERIOD}",
            f"/api/public/export/categories.csv?period={PERIOD}",
            "/static/site.css",
            "/robots.txt",
            "/healthz",
        ):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn("content-security-policy", {k.lower() for k in headers})
                self.assertTrue(body)

    def test_security_headers_are_sent(self) -> None:
        _status, headers, _body = self.request("GET", "/")
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertIn("default-src 'none'", lowered["content-security-policy"])
        self.assertEqual(lowered["x-content-type-options"], "nosniff")
        self.assertEqual(lowered["x-frame-options"], "DENY")

    def test_unknown_page_and_bad_period(self) -> None:
        status, _headers, _body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        status, _headers, body = self.request("GET", "/money?period=2025-07-05")
        self.assertEqual(status, 400)
        self.assertNotIn("acc_", body)

    def test_write_methods_are_refused(self) -> None:
        status, _headers, _body = self.request("POST", "/")
        self.assertEqual(status, 405)

    def test_etag_is_the_release_id(self) -> None:
        release = self.fixture.site.index.current()
        assert release is not None
        _status, headers, _body = self.request("GET", f"/api/public/money?period={PERIOD}")
        self.assertEqual(headers.get("ETag"), f'"{release.release_id}-money"')

    def test_json_api_is_leak_checked(self) -> None:
        from randreport.projection.privacy import assert_no_leaks

        for period in ("2025-07", "2025", "all"):
            _status, _headers, body = self.request("GET", f"/api/public/overview?period={period}")
            assert_no_leaks(json.loads(body))


if __name__ == "__main__":
    unittest.main()
