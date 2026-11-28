"""Tests for the redesigned public pages: the monthly basement leaflet.

The eight pages are no longer a copy of the design master; they are an own
composition built on the DTOs that already existed. What this module pins down:

* the leaflet furniture is on every page (masthead, ruled fields, one number
  strip, a footer that says when the data is refreshed) and the master's
  demo-only scaffolding is nowhere;
* the page emits no inline ``style``/``script``, so the deployed
  ``style-src 'self'`` policy cannot strip the layout;
* the four money states are told apart — an unallocated part never prints a
  classified ``0.00 ₽`` as if nothing had been spent, and a confirmed zero stays
  a plain ``0 ₽`` (the regression reported on the home page);
* a coverage percentage is drawn only when it exists, and is stamped
  ``предварительно`` while the snapshot is not final;
* every money value reads as Russian money (space thousands, comma kopecks);
* every printed percentage reads in the same Russian form as the money next to
  it — comma, one digit, one plain space, ``3,2 %`` (docs/PERCENT_FORMAT.md), on
  every surface, in the chart's legend and ``aria`` label, and in the
  «требуют классификации» row; the guard is proven to fail if the old dotted
  form ever comes back.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from randreport.domain import finance
from randreport.domain.dto import (
    CategoryDTO,
    ChronicleDTO,
    CommunityDTO,
    EventsDTO,
    MoneyDTO,
    NewsItemDTO,
    PeriodDTO,
)
from randreport.web import render
from randreport.web.app import PublicSite
from tests import dev_release

PERIOD = "2025-07"
ROUTES = ("/", "/money", "/events", "/community", "/support", "/chronicle", "/reports", "/methodology")
#: Surfaces the rule is checked on, including the archive the leaflet does not
#: link by default (docs/PERCENT_FORMAT.md section 6, item 1).
PERCENT_ROUTES = ("/", "/money", "/support", "/events", "/reports", "/reports/2025", "/reports/2025-01")
#: Archive routes ``SiteTestCase`` does not serve in its default set.
ARCHIVE_ROUTES = ("/reports/2025", "/reports/2025-01")

#: The one accepted form: comma, exactly one digit, one plain space, then ``%``.
ONE_PERCENT_FORM = re.compile(r"\d+,\d %")
#: Any printed percentage, whatever decimal separator it uses (includes NBSP).
ANY_PERCENT_FORM = re.compile(r"\d+[.,]\d\s*%")
#: The form this card removed: a dot as the decimals separator of a percentage.
DOTTED_PERCENT_FORM = re.compile(r"\d+\.\d+\s*%")
#: The same defect on a sum — a dot between rubles and kopecks.
DOTTED_MONEY_FORM = re.compile(r"\d+\.\d+ ₽")
TABLE_BLOCK = re.compile(r"<table\b[^>]*>.*?</table>", re.S)


def dotted_percents(html: str) -> list[str]:
    """Every percentage of the old form that survived in ``html``."""

    return DOTTED_PERCENT_FORM.findall(html)


def percent_tokens(html: str) -> list[str]:
    """Every printed percentage of ``html``, whatever its separator."""

    return ANY_PERCENT_FORM.findall(html)


#: The owner's approved compact chart lettering («61,5%», «91%», «100%»).
OWNER_CHART_FORM = re.compile(r"\d+(?:,\d)?%")
_OWNER_CHART_SVG = re.compile(r'<svg viewBox="-12 -10 820 205".*?</svg>', re.S)
_OWNER_CAT_SHARE = re.compile(r'<span class="cat-share">[^<]*</span>')
_OWNER_BAR_SCALE = '<div class="bar-scale"><span>0</span><span>100%</span></div>'


def split_owner_chart_annotations(html: str) -> tuple[str, list[str]]:
    """Split the money sheet into strict statistics and the owner's chart lettering.

    The strict «N,N %» statistic form keeps governing every printed share (the
    hidden chart table, the audit numbers); the compact form belongs to the
    poster's own lettering: the grey share notes in the ledger, the trend point
    labels and the bar scale (docs/PERCENT_FORMAT.md keeps the statistics rule).
    """

    shares = OWNER_CHART_FORM.findall(html)  # first pass, then strip them out
    svg = _OWNER_CHART_SVG.search(html)
    points = OWNER_CHART_FORM.findall(svg.group(0)) if svg else []
    rest = _OWNER_CAT_SHARE.sub('', html)
    if svg:
        rest = rest.replace(svg.group(0), '')
    rest = rest.replace(_OWNER_BAR_SCALE, '')
    # «shares» here over-collects (it also matches strict tokens); recompute the
    # compact set from the removed zones instead
    compact = [t for t in shares if t.endswith('%') and ' ' not in t]
    compact += points
    return rest, compact


def unclassified_category(direction: str, kopecks: int, share: str) -> CategoryDTO:
    """One «требуют классификации» row with a pinned share."""

    return CategoryDTO(
        key=f"k_unclassified_{direction}",
        label=finance.UNCLASSIFIED_LABEL,
        bucket=finance.UNCLASSIFIED_BUCKET,
        direction=direction,
        amount_kopecks=kopecks,
        amount_rub=render.format_kopecks(kopecks),
        share_percent=share,
        operations_count=None,
    )

#: Leftovers of the design master. None of them may reach a served page.
MASTER_ONLY = (
    "Культура держится на нас",
    "Сначала считаем, потом хвастаемся",
    "Общее дело — не бизнес-план",
    "Не прибыль. Общее дело.",
    "Подвал не пустует",
    "Спасибо, что вы с нами",
    "РЕЖИМ МАКЕТА",
    "ДИЗАЙН-ЭТАЛОН",
    "ДИЗАМБИГУАЦИЯ",
    "data-mode",
    "design-master",
    "35 000",
    "100 000",
    "6 682",
)


def money_dto(
    metrics: dict,
    *,
    completeness: str = "complete",
    categories: tuple[CategoryDTO, ...] = (),
) -> MoneyDTO:
    return MoneyDTO(
        period=PeriodDTO(
            requested=PERIOD,
            covered_start=date(2025, 7, 1),
            covered_end=date(2025, 7, 31),
            complete=True,
        ),
        as_of=date(2025, 7, 31),
        release_id="rel_leaflet",
        freshness="fresh",
        verification="operational",
        completeness=completeness,
        source_labels=("zenmoney",),
        unit="RUB_kopecks",
        metrics=metrics,
        categories=categories,
    )


def category(label: str, kopecks: int, *, bucket: str, direction: str, share: str | None = "10.0") -> CategoryDTO:
    return CategoryDTO(
        key=f"k{abs(hash(label)) % 10**10}",
        label=label,
        bucket=bucket,
        direction=direction,
        amount_kopecks=kopecks,
        amount_rub=render.format_kopecks(kopecks),
        share_percent=share,
        operations_count=1,
    )


class SiteTestCase(unittest.TestCase):
    """One synthetic release on disk, served through the real WSGI callable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        root = Path(cls.tmpdir.name)
        cls.release_dir = root / "release"
        dev_release.build(str(cls.release_dir), period=PERIOD, db_path=str(root / "build.sqlite"))
        cls.site = PublicSite(release_dir=cls.release_dir)
        cls.pages: dict[str, str] = {}
        for route in ROUTES:
            response = cls.site._dispatch(
                {"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": f"period={PERIOD}"}
            )
            cls.pages[route] = response.text

    @classmethod
    def tearDownClass(cls) -> None:
        cls.site.close()
        cls.tmpdir.cleanup()


class TestLeafletFurniture(SiteTestCase):
    def test_every_page_carries_the_masthead_navigation_and_footer(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route):
                text = self.pages[route]
                # The owner's logo replaces the decorative asterisk on the home
                # poster; every other sheet keeps the asterisk brand mark.
                if route == '/money':
                    self.assertIn('src="/static/brand-real-logo.webp"', text)
                    self.assertIn('aria-label="Разделы РанДома"', text)
                    self.assertIn('<main id="main" tabindex="-1">', text)
                    self.assertIn('обновление в 03:00 МСК', text)
                    self.assertIn('<footer class="endnote">', text)
                    continue
                if route == '/':
                    self.assertIn('<img class="brand-mark" src="/static/brand-logo.png"', text)
                    self.assertNotIn('class="star"', text)
                else:
                    self.assertIn('<span class="star" aria-hidden="true">✳</span>', text)
                self.assertIn('aria-label="Разделы сайта"', text)
                self.assertRegex(text, r'<main id="content" tabindex="-1"(?: class="poster")?>')
                self.assertIn("Обновляем раз в сутки, в 03:00 по Москве", text)
                self.assertIn('<footer class="footer">' if route == '/' else '<footer class="site-footer">', text)

    def test_every_page_has_exactly_one_h1_and_a_lede(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route):
                text = self.pages[route]
                # the collage h1 carries an aria-label with the whole question, so
                # the tag may have attributes; what matters is that there is one
                self.assertEqual(len(re.findall(r"<h1[ >]", text)), 1)
                if route == '/':
                    self.assertEqual(text.count('<p class="intro-line">'), 1)
                    self.assertNotIn('class="lead"', text)  # no duplicate intro on poster
                    continue
                if route == '/money':
                    self.assertIn('class="small-rubric"', text)
                    self.assertIn('class="one-liner"', text)
                    continue
                self.assertRegex(text, r'class="[^"]*\beyebrow\b', f"{route} has no eyebrow line")
                self.assertIn('class="lead"', text)

    def test_no_page_repeats_the_design_master(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route):
                text = self.pages[route]
                for leftover in MASTER_ONLY:
                    self.assertNotIn(leftover, text, f"{route} still contains {leftover!r}")

    def test_no_inline_style_or_script_survives_the_content_security_policy(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route):
                text = self.pages[route]
                self.assertNotIn("<style", text)
                self.assertNotIn("<script", text)
                self.assertNotIn("style=\"", text)

    def test_the_clock_is_the_documented_one_and_nothing_else(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route):
                times = set(re.findall(r"\b\d{1,2}:\d{2}\b", self.pages[route]))
                self.assertLessEqual(times, {"03:00"}, f"{route} states another time: {times}")

    def test_money_on_a_page_reads_as_russian_money(self) -> None:
        self.assertEqual(render.rub_from_kopecks(6508900), "65 089,00 ₽")
        self.assertEqual(render.rub_from_kopecks(0), "0,00 ₽")
        self.assertEqual(render.rub_from_kopecks(-4558900), "-45 589,00 ₽")
        self.assertEqual(render.rub_from_kopecks(None), "нет данных")
        # the poster sums follow the owner's approved compact form; the
        # disclosure keeps the precise leaflet convention next to them
        self.assertIn('data-field="compensation_kopecks">19 500 ₽', self.pages["/money"])
        self.assertIn("45 589,00 ₽", self.pages["/money"])


class TestStatesOnTheHomeLeaflet(unittest.TestCase):
    """The regression: a partial snapshot must not print a classified zero."""

    def home(self, money: MoneyDTO | None, *, historical: dict | None = None) -> str:
        return render.home_section(
            period_label=PERIOD,
            money=money,
            events=None,
            community=None,
            chronicle=None,
            historical=historical or {},
        )

    def test_partial_snapshot_never_prints_a_classified_zero(self) -> None:
        # classification has not run yet: nothing is allocated, but 1 234,56 ₽ did
        # move through the accounts. "0.00 ₽ of expenses" would be a lie.
        partial = money_dto(
            {
                "expense_kopecks": 0,
                "compensation_kopecks": 0,
                "unclassified_in_kopecks": 123456,
                "unclassified_out_kopecks": 0,
                "coverage_is_final": False,
                "subsidy_kopecks": None,
                "balance_kopecks": 556340,
            },
            completeness="partial",
        )
        html = self.home(partial)
        self.assertNotIn("0,00 ₽", html)
        self.assertNotIn("0.00 ₽", html)
        self.assertIn("не разнесено", html)
        self.assertIn("1 234,56 ₽", html)
        # the balance is still known and is still not called profit
        self.assertIn("5 563,40 ₽", html)
        self.assertIn("не прибыль", html)

    def test_a_partial_snapshot_does_not_claim_a_coverage_of_zero(self) -> None:
        partial = money_dto(
            {"expense_kopecks": 100000, "coverage_percent": "0.0", "coverage_is_final": False},
            completeness="partial",
        )
        html = self.home(partial)
        self.assertIn("Покрытие пока не считаем честно", html)
        self.assertNotIn('<svg class="coverage-bar"', html)
        self.assertNotIn("0,0 %", html)

    def test_a_confirmed_zero_stays_a_plain_zero(self) -> None:
        complete = money_dto(
            {
                "expense_kopecks": 0,
                "compensation_kopecks": 0,
                "unclassified_in_kopecks": 0,
                "unclassified_out_kopecks": 0,
                "coverage_is_final": True,
                "subsidy_kopecks": None,
                "balance_kopecks": 0,
            },
            completeness="complete",
        )
        html = self.home(complete)
        self.assertIn("Подтверждённых трат за период не было", html)
        self.assertIn("0 ₽", html)
        self.assertNotIn("не разнесено", html)

    def test_a_complete_nonzero_period_shows_plain_amounts(self) -> None:
        complete = money_dto(
            {
                "expense_kopecks": 6508900,
                "compensation_kopecks": 1950000,
                "unclassified_in_kopecks": 0,
                "unclassified_out_kopecks": 0,
                "coverage_is_final": True,
                "coverage_percent": "30.0",
                "subsidy_kopecks": 800000,
                "balance_kopecks": 556340,
                "uncovered_kopecks": 4558900,
            },
            completeness="complete",
            categories=(category("Донаты", 1200000, bucket="external_compensation", direction="in", share="61.5"),),
        )
        html = self.home(complete)
        self.assertIn("65 089,00 ₽", html)
        self.assertIn("полный период", html)
        self.assertNotIn("не разнесено", html)

    def test_an_absent_snapshot_is_an_explained_empty_state_not_zeros(self) -> None:
        html = self.home(None)
        self.assertIn("Финансового снимка за этот период нет", html)
        self.assertNotIn("0,00 ₽", html)

    def test_a_historical_period_is_shown_as_an_aggregate_with_its_caveat(self) -> None:
        from randreport import history
        from randreport.domain.timeframes import year_period

        views = {
            "events": history.metric_view(year_period("2025"), "events"),
            "receipts": history.metric_view(year_period("2025"), "receipts"),
            "expenses": history.metric_view(year_period("2025"), "expenses"),
        }
        html = self.home(None, historical=views)
        self.assertIn("707 920 ₽", html)
        self.assertIn("не остаток", html)
        self.assertNotIn("0,00 ₽", html)


class TestCoverageAndEventsRendering(unittest.TestCase):
    def test_money_page_stamps_a_preliminary_percentage(self) -> None:
        money = money_dto(
            {
                "expense_kopecks": 1000000,
                "compensation_kopecks": 300000,
                "coverage_percent": "30.0",
                "coverage_is_final": False,
                "unclassified_out_kopecks": 5000,
            },
            completeness="partial",
            categories=(category("Донаты", 300000, bucket="external_compensation", direction="in", share="30.0"),),
        )
        html = render.money_section(money, period_label=PERIOD, historical={})
        self.assertIn('class="stamp stamp-rust">предварительно', html)
        self.assertIn("Покрытие расходов: <strong>30,0 %</strong>", html)
        self.assertIn("недостающая часть", html)

    def test_money_page_refuses_to_draw_a_percentage_it_does_not_have(self) -> None:
        money = money_dto(
            {"expense_kopecks": None, "compensation_kopecks": None, "coverage_is_final": False},
            completeness="partial",
        )
        html = render.money_section(money, period_label=PERIOD, historical={})
        self.assertIn("Покрытие пока не считаем честно", html)
        self.assertNotIn('<svg class="coverage-bar"', html)

    def test_closed_events_leave_no_trace_but_are_counted(self) -> None:
        response = self._events_page()
        self.assertIn("только агрегат", response)
        self.assertNotIn("Закрытая встреча коммунаров", response)
        self.assertNotIn("Внутреннее собрание", response)

    def _events_page(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            dev_release.build(str(Path(tmp) / "release"), period=PERIOD, db_path=str(Path(tmp) / "b.sqlite"))
            site = PublicSite(release_dir=Path(tmp) / "release")
            try:
                return site._dispatch(
                    {"REQUEST_METHOD": "GET", "PATH_INFO": "/events", "QUERY_STRING": f"period={PERIOD}"}
                ).text
            finally:
                site.close()

    def test_calendar_marks_only_public_days_and_says_so(self) -> None:
        page = self._events_page()
        self.assertIn("2025-07-05", page)
        self.assertIn("2025-07-12", page)
        self.assertNotIn("2025-07-15", page)  # a closed date leaves no cell mark
        self.assertIn("Дни без отметки могут содержать закрытые события", page)

    def test_chronicle_is_a_dated_log_with_sources(self) -> None:
        chronicle = ChronicleDTO(
            period=PeriodDTO(requested=PERIOD, covered_start=date(2025, 7, 1), covered_end=date(2025, 7, 31), complete=True),
            as_of=date(2025, 7, 31),
            release_id="rel_leaflet",
            freshness="fresh",
            verification="operational",
            completeness="complete",
            source_labels=("telegram",),
            news_items=(
                NewsItemDTO(
                    id="n1",
                    title="Покрасили стены",
                    summary="Коротко о ремонте.",
                    category="ремонт",
                    published_on=date(2025, 7, 2),
                    source_urls=("https://t.me/random_culture/700",),
                    machine_processed=True,
                    verified=False,
                ),
            ),
            metrics={},
        )
        html = render.chronicle_section(chronicle)
        self.assertIn('<ol class="log">', html)
        self.assertIn("2025-07-02", html)
        self.assertIn('rel="noopener"', html)
        self.assertIn("не проверено вручную", html)

    def test_an_empty_chronicle_explains_itself(self) -> None:
        html = render.chronicle_section(None)
        self.assertIn("Записей летописи за этот период нет", html)
        self.assertNotIn("0 новостей", html)


class TestPeriodLinks(SiteTestCase):
    def test_navigation_and_switcher_carry_the_period_on_every_page(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route):
                text = self.pages[route]
                self.assertIn(f'href="/money?period={PERIOD}"', text)
                self.assertIn(f'href="/methodology?period={PERIOD}"', text)
                self.assertIn(f'<option value="{PERIOD}" selected>', text)
                self.assertRegex(text, r'<select id="period-select" name="period"( aria-label="[^"]*")?>')
                self.assertIn('<label for="period-select">', text)

    def test_the_archive_index_links_every_published_period(self) -> None:
        text = self.pages["/reports"]
        for label in ("2025-01", "2025-02", "2025-03", "2025", "2025-04..2025-08"):
            with self.subTest(label=label):
                self.assertIn(f'href="/reports/{label}">', text)

    def test_every_internal_link_is_a_real_same_origin_path(self) -> None:
        for route in ROUTES:
            with self.subTest(route=route):
                hrefs = re.findall(r'href="([^"]+)"', self.pages[route])
                for href in hrefs:
                    self.assertTrue(
                        href.startswith("/") or href.startswith("#") or href.startswith("https://t.me/"),
                        f"{route} has a link outside the site: {href!r}",
                    )


class TestKeyboardAndCss(SiteTestCase):
    def test_skip_link_main_landmark_and_focus_outlines(self) -> None:
        css = self.site._dispatch(
            {"REQUEST_METHOD": "GET", "PATH_INFO": "/static/site.css", "QUERY_STRING": ""}
        ).text
        self.assertIn(":focus-visible", css)
        self.assertIn("overflow-x: auto", css)
        for breakpoint in ("max-width: 640px", "max-width: 480px"):
            self.assertIn(breakpoint, css)
        for route in ROUTES:
            with self.subTest(route=route):
                anchor = '#main' if route == '/money' else '#content'
                self.assertIn(f'<a class="skip" href="{anchor}">', self.pages[route])

    def test_labels_and_captions_carry_the_meaning_of_each_table(self) -> None:
        text = self.pages["/money"]
        self.assertIn("<caption>", text)
        self.assertIn("текстовая версия графика", text)
        self.assertIn('scope="col"', text)

    def test_the_pages_never_print_an_internal_flow_identifier(self) -> None:
        # the flow class is a machine word; the reader gets the human phrase
        for route in ROUTES:
            with self.subTest(route=route):
                text = self.pages[route]
                for machine_word in (
                    "external_compensation",
                    "operating_expense",
                    "commune_subsidy",
                    "balance_adjustment",
                    "expense_refund",
                    "unclassified_present",
                    "subsidy_unknown",
                    "rel_",
                ):
                    self.assertNotIn(machine_word, text, f"{route} prints the internal word {machine_word!r}")
        self.assertIn("от деятельности и поддержки", self.pages["/money"])

    def test_the_category_tables_state_their_own_totals(self) -> None:
        # the rows of a table and the ledger figure above it are different sums
        # (compensation excludes the communards' own contribution and refunds,
        # expenses are net of refunds) — so each table names its total explicitly.
        text = self.pages["/money"]
        self.assertIn('id="income-sum" data-field="compensation_kopecks">19 500 ₽', text)
        self.assertIn('id="expense-sum" data-field="expense_kopecks">65 089 ₽', text)
        # the ledger rows keep the precise leaflet money form
        self.assertIn("Разнесено по категориям<span class=\"cat-value\">23 000,00 ₽", text)
        self.assertIn("Разнесено по категориям<span class=\"cat-value\">65 589,00 ₽", text)

    def test_a_long_field_note_cannot_widen_the_page(self) -> None:
        # A field note may carry a real headline ("Мертвые не тлеют: как ожившие
        # мертвецы попали в кинематограф…").  Locking it to its own width pushed
        # the home sheet past a 390/320 px viewport (measured in a real browser
        # against the deployed release), so the rule must let it shrink and break.
        css = self.site._dispatch(
            {"REQUEST_METHOD": "GET", "PATH_INFO": "/static/site.css", "QUERY_STRING": ""}
        ).text
        rule = re.search(r"\.field-note\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "the .field-note rule is gone")
        block = rule.group(1)
        self.assertNotIn("flex: none", block)
        self.assertIn("min-width: 0", block)
        self.assertIn("overflow-wrap: anywhere", block)


class PercentFormAssertions(unittest.TestCase):
    """The three guards of docs/PERCENT_FORMAT.md section 6.

    They live apart from a test class because the same guards are asserted by the
    tests that check the leaflet *and* by the test that proves them to fail once
    the old dotted form comes back (section 6, item 5): one definition, so the
    proof cannot pass against a weaker check than the one that runs for real.
    """

    def assert_one_percent_form(self, html: str, surface: str) -> None:
        """No dotted percentage, and every printed one in the accepted form."""

        offenders = dotted_percents(html)
        self.assertEqual([], offenders, f"{surface} prints the old percent form {offenders}")
        self.assertEqual([], DOTTED_MONEY_FORM.findall(html), f"{surface} prints a dotted sum")
        for token in percent_tokens(html):
            self.assertTrue(
                ONE_PERCENT_FORM.fullmatch(token),
                f"{surface} prints {token!r}: the rule is comma, one digit, one plain space (docs/PERCENT_FORMAT.md §1)",
            )

    def assert_one_separator_per_table(self, html: str, surface: str) -> None:
        """Inside one table, shares and sums use one decimal separator."""

        for index, table in enumerate(TABLE_BLOCK.findall(html), start=1):
            separators = set(re.findall(r"\d+([.,])\d\s*%", table))
            separators |= set(re.findall(r"\d+([.,])\d{2} ₽", table))
            self.assertLessEqual(
                separators,
                {","},
                f"{surface}, table #{index} mixes decimal separators: {sorted(separators)}",
            )

    def assert_documented_unclassified_rows(self, html: str) -> None:
        """The two rows of «требуют классификации» exactly as the documents quote them."""

        self.assertIn(UNCLASSIFIED_IN_ROW, html, "the receipts row of «требуют классификации» is not the documented one")
        self.assertIn(UNCLASSIFIED_OUT_ROW, html, "the expenses row of «требуют классификации» is not the documented one")


def unclassified_money() -> MoneyDTO:
    """The two documented «требуют классификации» rows as a period of its own.

    The shares (3,2 % of receipts, 96,8 % of expenses) and the sums (2,28 ₽ and
    68,80 ₽) are the ones ``docs/TEST_RESULTS_RELEASE.md`` §5 and
    ``docs/PERCENT_FORMAT.md`` §8 quote from the live release
    (``/money?period=2026-11``). The test pins the documented values instead of
    whatever the synthetic fixture happens to compute.
    """

    return money_dto(
        {
            "expense_kopecks": 7107,
            "compensation_kopecks": 7125,
            "coverage_percent": "30.0",
            "coverage_is_final": True,
            "unclassified_in_kopecks": 228,
            "unclassified_out_kopecks": 6880,
            "subsidy_kopecks": None,
            "balance_kopecks": 556340,
            "uncovered_kopecks": 4558900,
        },
        completeness="complete",
        categories=(
            unclassified_category("in", 228, "3.2"),
            unclassified_category("out", 6880, "96.8"),
        ),
    )


#: The receipts row of the live release, byte for byte (docs/PERCENT_FORMAT.md §8).
UNCLASSIFIED_IN_ROW = (
    '<tr><th scope="row"><span class="cat-name">требуют классификации</span>'
    '<span class="cat-kind">категория требует проверки</span>'
    '<svg class="minibar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true">'
    '<rect x="0" y="0" width="3.2" height="4"></rect></svg></th>'
    '<td class="num">2,28 ₽</td><td class="num">3,2 %</td><td class="num">—</td></tr>'
)
#: The expenses row of the live release, byte for byte.
UNCLASSIFIED_OUT_ROW = (
    '<tr><th scope="row"><span class="cat-name">требуют классификации</span>'
    '<span class="cat-kind">категория требует проверки</span>'
    '<svg class="minibar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true">'
    '<rect x="0" y="0" width="96.8" height="4"></rect></svg></th>'
    '<td class="num">68,80 ₽</td><td class="num">96,8 %</td><td class="num">—</td></tr>'
)


class TestOnePercentFormAcrossTheLeaflet(PercentFormAssertions, SiteTestCase):
    """The regression for docs/PERCENT_FORMAT.md §6: one form, on every surface.

    The pages are served from a real synthetic release through the real WSGI
    callable, so the check sees what a visitor's browser gets — tables included.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        for route in ARCHIVE_ROUTES:
            response = cls.site._dispatch(
                {"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": f"period={PERIOD}"}
            )
            cls.pages[route] = response.text

    def test_no_surface_prints_a_dot_as_the_decimal_separator_of_a_percentage(self) -> None:
        for route in PERCENT_ROUTES:
            with self.subTest(route=route):
                html = self.pages[route]
                if route == "/money":
                    # the owner's compact chart lettering is checked separately
                    html, _compact = split_owner_chart_annotations(html)
                self.assert_one_percent_form(html, route)

    def test_every_printed_percentage_uses_one_digit_and_a_plain_space(self) -> None:
        # the rule is narrower than "a comma somewhere": exactly one digit after
        # it and a plain space before ``%`` (an NBSP or two digits is a defect).
        for route in PERCENT_ROUTES:
            with self.subTest(route=route):
                html = self.pages[route]
                if route == "/money":
                    html, compact = split_owner_chart_annotations(html)
                    for token in compact:
                        self.assertTrue(
                            OWNER_CHART_FORM.fullmatch(token),
                            f"/money chart lettering prints {token!r}: the owner's compact form is digits, optional comma digit, %",
                        )
                for token in percent_tokens(html):
                    self.assertTrue(ONE_PERCENT_FORM.fullmatch(token), f"{route} prints {token!r}")

    def test_the_surfaces_that_print_shares_print_the_comma_form(self) -> None:
        self.assertIn("Итого покрытие", self.pages["/money"])
        self.assertTrue(ONE_PERCENT_FORM.search(self.pages["/money"]), "/money prints no share in the accepted form")
        self.assertTrue(ONE_PERCENT_FORM.search(self.pages["/"]), "/ prints no share in the accepted form")
        self.assertTrue(ONE_PERCENT_FORM.search(self.pages["/reports/2025"]), "the yearly archive prints no covered share")

    def test_no_table_mixes_a_share_and_a_sum_in_two_separators(self) -> None:
        for route in ("/money", "/", "/reports/2025"):
            with self.subTest(route=route):
                self.assert_one_separator_per_table(self.pages[route], route)

    def test_owner_sheet_keeps_percent_and_money_in_separate_blocks(self) -> None:
        html = self.pages["/money"]
        self.assertRegex(html, r'<table class="chart-alt">[\s\S]*?30,0 %')
        # the ledger rows print the precise leaflet money form, never percentages
        self.assertRegex(html, r'class="cat-value">[0-9 ]+,\d{2} ₽')

    def test_the_coverage_chart_legend_and_aria_label_use_the_one_form(self) -> None:
        html = self.pages["/money"]
        figure = re.search(r'<figure class="coverage">.*?</figure>', html, re.S)
        if figure is None:
            self.fail("the coverage chart is gone from /money")
        block = figure.group(0)
        aria = re.search(r'class="coverage-bar"[^>]*aria-label="([^"]*)"', block)
        if aria is None:
            self.fail("the coverage chart carries no aria label")
        legend = re.search(r'<ul class="coverage-legend">.*?</ul>', block, re.S)
        if legend is None:
            self.fail("the coverage chart carries no legend")
        for name, fragment in (("aria label", aria.group(1)), ("legend", legend.group(0))):
            with self.subTest(part=name):
                tokens = percent_tokens(fragment)
                self.assertTrue(tokens, f"the coverage {name} prints no share")
                for token in tokens:
                    self.assertTrue(ONE_PERCENT_FORM.fullmatch(token), f"the coverage {name} prints {token!r}")
                self.assertEqual([], dotted_percents(fragment), f"the coverage {name} prints the old form")
        # every number the chart *says* in its label is the one its text table
        # prints: the aria label describes the segments, the table adds the
        # missing part and the total.
        chart_table = re.search(r'<table class="chart-alt">.*?</table>', html, re.S)
        if chart_table is None:
            self.fail("the text version of the chart is gone")
        table_tokens = set(percent_tokens(chart_table.group(0)))
        self.assertTrue(set(percent_tokens(aria.group(1))) <= table_tokens, "the chart's aria label names a share its table does not")
        self.assertTrue(set(percent_tokens(legend.group(0))) <= table_tokens, "the chart's legend names a share its table does not")
        self.assertIn("70,0 %", table_tokens)  # the missing part lives in the table


class TestTheCoverageChartStringForm(unittest.TestCase):
    """The exact strings of the chart, so the form cannot drift by a wording change."""

    def rendered(self, segments, *, total, **kwargs) -> str:
        return render.coverage_bar(segments, total_percent=total, **kwargs)

    def test_the_legend_the_aria_label_and_the_text_table_share_the_form(self) -> None:
        html = self.rendered(
            [("Донаты", Decimal("18.4")), ("Доля с ивента", Decimal("9.2")), ("Склад", Decimal("2.3"))],
            total=Decimal("30.0"),
            state_note="предварительно",
        )
        self.assertIn("Покрытие расходов: <strong>30,0 %</strong>", html)
        self.assertIn('<span class="stamp stamp-rust">предварительно</span>', html)
        self.assertIn(
            "Покрытие расходов поступлениями: 30,0 %. Донаты: 18,4 %; Доля с ивента: 9,2 %; Склад: 2,3 %", html
        )
        self.assertIn('<li><span class="swatch seg-0" aria-hidden="true"></span>Донаты 18,4 %</li>', html)
        self.assertIn(
            '<li><span class="swatch seg-missing" aria-hidden="true"></span>недостающая часть 70,0 %</li>', html
        )
        self.assertIn('<tr><th scope="row">Донаты</th><td class="num">18,4 %</td></tr>', html)
        self.assertIn('<tr><th scope="row">Недостающая часть</th><td class="num">70,0 %</td></tr>', html)
        self.assertIn('<tr><th scope="row">Итого покрытие</th><td class="num">30,0 %</td></tr>', html)
        # the geometry stays numeric: an SVG attribute is not printed text (§2.4)
        self.assertIn('width="18.46"', html)
        self.assertIn('width="70.0"', html)
        self.assertEqual([], dotted_percents(html))
        self.assertEqual([], DOTTED_MONEY_FORM.findall(html))

    def test_an_unknown_share_keeps_the_chart_word(self) -> None:
        # the number's form is shared with the rest of the page; the *word* for an
        # unknown share stays the chart's own one (docs/PERCENT_FORMAT.md §1).
        html = self.rendered(
            [("Донаты", None), ("Закрытые мероприятия", Decimal("0.4"))],
            total=Decimal("0.4"),
            missing_label="прочее",
        )
        self.assertIn("Донаты: нет данных", html)
        self.assertIn('<tr><th scope="row">Донаты</th><td class="num">нет данных</td></tr>', html)
        self.assertIn("Покрытие расходов: <strong>0,4 %</strong>", html)
        self.assertIn("Закрытые мероприятия 0,4 %", html)
        self.assertNotIn("не применимо", html)
        self.assertEqual([], dotted_percents(html))


class TestTheUnclassifiedRow(PercentFormAssertions, unittest.TestCase):
    """The row the owner quoted: docs/TEST_RESULTS_RELEASE.md §5, docs/PERCENT_FORMAT.md §8."""

    def rendered(self) -> str:
        return render.money_section(unclassified_money(), period_label=PERIOD, historical={})

    def test_the_documented_rows_are_printed(self) -> None:
        html = self.rendered()
        self.assert_documented_unclassified_rows(html)
        self.assertIn("требуют классификации", html)
        self.assertEqual([], dotted_percents(html))

    def test_the_share_prints_with_a_comma_while_the_bar_stays_numeric(self) -> None:
        # 3,2 % is text, width="3.2" is geometry (docs/PERCENT_FORMAT.md §2.4):
        # a Russian string in that attribute would break the bar.
        html = self.rendered()
        self.assertIn('<td class="num">3,2 %</td>', html)
        self.assertIn('<td class="num">96,8 %</td>', html)
        self.assertIn('width="3.2"', html)
        self.assertIn('width="96.8"', html)
        self.assertNotIn('width="3,2"', html)
        self.assertNotIn('<td class="num">3.2 %</td>', html)


class TestTheGuardCatchesTheOldFormat(PercentFormAssertions, SiteTestCase):
    """docs/PERCENT_FORMAT.md §6, item 5: the old format must fail these tests.

    The formatter is patched, not the assertions: the same guards that run for
    real are exercised against a page built by a renderer that puts the dot back.
    """

    @staticmethod
    def _dotted_format_percent(value, *, digits: int = 1):
        """The behaviour before ``f387674``: the same number, dotted."""

        # imported here so this module still imports on the pre-fix tree, where
        # the single formatter does not exist yet
        from randreport.domain.money import format_percent as money_format_percent

        rendered = money_format_percent(value, digits=digits)
        return None if rendered is None else rendered.replace(",", ".")

    def dotted_page(self, route: str = "/money") -> str:
        with mock.patch.object(render, "format_percent", self._dotted_format_percent):
            return self.site._dispatch(
                {"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": f"period={PERIOD}"}
            ).text

    def test_a_return_to_the_dot_is_caught_by_every_guard(self) -> None:
        html = self.dotted_page()
        offenders = dotted_percents(html)
        self.assertTrue(offenders, "the patched renderer printed no old-form percentage at all")
        self.assertIn("70,0", self.pages["/money"], "sanity: the unpatched page prints the share")
        with self.assertRaises(AssertionError):
            self.assert_one_percent_form(html, "/money")
        with self.assertRaises(AssertionError):
            self.assert_one_separator_per_table(html, "/money")
        with mock.patch.object(render, "format_percent", self._dotted_format_percent):
            fixed_row = render.money_section(unclassified_money(), period_label=PERIOD, historical={})
        with self.assertRaises(AssertionError):
            self.assert_documented_unclassified_rows(fixed_row)


if __name__ == "__main__":
    unittest.main()
