"""The leaflet shows the approved category hints and the allocation explanation.

Card **t_7fad32c2**.  The two facts already live in the DTOs the JSON and the CSV
carry — ``CategoryDTO.hint`` and ``MoneyDTO.allocation`` — and this module pins
what the *HTML* is allowed to do with them:

* an approved hint is rendered, an absent one is not (an empty disclosure would
  be noise for a screen reader, an invented one would be a lie);
* the disclosure is a native ``<details>``/``<summary>``: focusable, tap- and
  Enter-activatable, and it needs no script under ``default-src 'none'``;
* the allocation explanation is built from the real ``AllocationDTO`` and sits
  next to the category totals, in the leaflet's own money convention and table
  furniture;
* no private identifier, key, account or operation id reaches a page, and the
  content security policy is the same one as before;
* the money states are untouched: the block never turns an unallocated part
  into a classified ``0 ₽``, and an unallocated remainder stays apart.

Everything is synthetic: the offline fixtures build a real release through the
real nightly cycle, and no real store, secret or private file is involved.
"""

from __future__ import annotations

import html as html_module
import json
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import cast

from randreport.domain.allocation import AllocationPolicy, RentRule, ShareRule
from randreport.domain.dto import (
    AllocationDTO,
    CategoryDTO,
    MoneyDTO,
    PeriodDTO,
    RentMonthDTO,
)
from randreport.domain.timeframes import month_period
from randreport.ops.pipeline import ReleaseBuilder
from randreport.store import apply_migrations, connect
from randreport.web import hints as web_hints
from randreport.web import render
from randreport.web.__main__ import ALLOWED_TIMES, PAGES, _LEAK_PATTERNS, _TIME_RE
from randreport.web.app import CSP, PublicSite
from tests import dev_release as dr
from tests import fixtures as fx
from tests import fixtures_ingest as fi

PERIOD = "2025-07"
ROUTES = PAGES

#: Four approved hints: three expense categories of the money page and one
#: income category that the support page repeats from the same DTO.
HINTS = {
    "cat_rent": "Аренда подвала по согласованной ставке",
    "cat_utilities": "Общие расходы и ЖКУ: подвал показывает свою долю",
    "cat_supplies": "Расходники и хозтовары пространства",
    "cat_donations": "Донаты от гостей и слушателей",
}

#: The owner's rules: a one-third share of shared expenses and the agreed rent.
ALLOCATION = AllocationPolicy(
    version="owner-synthetic-1",
    share_rules={"cat_utilities": ShareRule(1, 3)},
    rent_category_ids=frozenset({"cat_rent"}),
    rent_rule=RentRule(9_000_000),
)


def build_release(release_dir: Path, db_path: Path, *, hints=None, allocation=None) -> str:
    """Run the real nightly cycle over the offline fixtures and publish a release."""

    policy = fx.policy(
        category_hints=HINTS if hints is None else hints,
        allocation=ALLOCATION if allocation is None else allocation,
    )
    conn = connect(str(db_path))
    try:
        apply_migrations(conn)
        zen, _ = fi.zenmoney(rows=dr.movements())
        cal, _ = fi.teamup(pages=[{"events": dr.calendar(), "pages": {"next": None}}])
        tg, _ = fi.telegram(
            posts=[fi.post("700", "Мы отремонтировали вход в подвал."), fi.post("701", "Анонс концерта")],
            members=4128,
        )
        ext = fi.extractor(conn, script=[fi.news_response(dr.DONOR_NEWS)])
        report = fi.run_once(
            conn,
            [
                fi.zenmoney_adapter(zen),
                fi.teamup_adapter(cal),
                fi.telegram_adapter(tg),
                fi.news_adapter(ext),
            ],
            release_builder=ReleaseBuilder(
                policy=policy, event_mapping=fx.event_mapping(), period=month_period(PERIOD)
            ),
            publish_dir=str(release_dir),
            lease=str(release_dir / "nightly.lock"),
            publish=True,
        )
        return report.release_id
    finally:
        conn.close()


class ServedReleaseTestCase(unittest.TestCase):
    """One synthetic release with approved hints, served through the WSGI callable."""

    hints: dict[str, str] = HINTS
    allocation: AllocationPolicy = ALLOCATION

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        root = Path(cls.tmpdir.name)
        cls.release_dir = root / "release"
        cls.release_id = build_release(
            cls.release_dir, root / "build.sqlite", hints=cls.hints, allocation=cls.allocation
        )
        cls.money_payload = json.loads(
            (cls.release_dir / cls.release_id / "money.json").read_text(encoding="utf-8")
        )
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

    # -- helpers ---------------------------------------------------------- #

    def hinted_categories(self) -> list[dict]:
        """The categories of the release that carry an approved hint."""

        return [c for c in self.money_payload["categories"] if c.get("hint")]

    def category_by_label(self, label: str) -> dict:
        matches = [c for c in self.money_payload["categories"] if c["label"] == label]
        self.assertTrue(matches, f"no category labelled {label!r} in the release")
        return matches[0]

    def disclosure(self, label: str, hint: str, *, summary: str = "подсказка") -> str:
        """The exact disclosure markup the leaflet must emit, escaped."""

        e = html_module.escape
        return (
            f'<details class="hint-disclosure"><summary aria-label="Подсказка: {e(label, quote=True)}">'
            f'{summary}</summary><span class="hint-text">{e(hint)}</span></details>'
        )

    def allocation_dto(self) -> AllocationDTO:
        """The allocation contract as the page reads it, straight from the release."""

        return cast(AllocationDTO, AllocationDTO.from_public_dict(self.money_payload["allocation"]))


class ApprovedHintTests(ServedReleaseTestCase):
    def test_the_release_really_carries_the_approved_hints(self) -> None:
        self.assertEqual(4, len(self.hinted_categories()))
        self.assertEqual(HINTS["cat_rent"], self.category_by_label("Аренда")["hint"])
        self.assertEqual(HINTS["cat_donations"], self.category_by_label("Донаты")["hint"])
        self.assertIsNone(self.category_by_label("Комиссии")["hint"])

    def test_an_approved_hint_is_rendered_next_to_its_own_category(self) -> None:
        money = self.pages["/money"]
        for category in self.hinted_categories():
            with self.subTest(category=category["label"]):
                # the owner's ledger marks the hint with a bare «?»
                self.assertIn(self.disclosure(category["label"], category["hint"], summary="?"), money)

    def test_the_support_page_carries_the_hint_of_a_category_it_renders(self) -> None:
        """The hint belongs to the category, not to one page."""

        donations = self.category_by_label("Донаты")
        support = self.pages["/support"]
        self.assertIn(self.disclosure(donations["label"], donations["hint"]), support)
        self.assertNotIn("Правила распределения подвала", support)

    def test_a_category_without_an_approved_hint_gets_no_disclosure(self) -> None:
        money = self.pages["/money"]
        unapproved = self.category_by_label("Комиссии")
        self.assertNotIn(f'Подсказка: {unapproved["label"]}"', money)
        # the name is still there, and the flow wording follows it directly
        self.assertIn(f'<span class="cat-name">{unapproved["label"]}</span><span class="cat-share">', money)
        # exactly the hinted categories carry a disclosure: no invented ones
        expected = len([c for c in self.hinted_categories() if c["bucket"] != "balance_adjustment"])
        self.assertEqual(expected, money.count('<details class="hint-disclosure"'))

    def test_the_hint_text_is_never_rendered_outside_its_disclosure(self) -> None:
        money = self.pages["/money"]
        for category in self.hinted_categories():
            shown = html_module.escape(category["hint"])
            with self.subTest(category=category["label"]):
                self.assertEqual(
                    money.count(shown),
                    money.count(f'<span class="hint-text">{shown}</span>'),
                    "a hint reached the page as plain prose, without its accessible disclosure",
                )

    def test_the_disclosure_is_a_native_summary_that_needs_no_script(self) -> None:
        money = self.pages["/money"]
        for category in self.hinted_categories():
            with self.subTest(category=category["label"]):
                self.assertIn(f'aria-label="Подсказка: {category["label"]}"', money)
                # the control itself carries no script hook and no negative tab
                # order: `<details>`/`<summary>` are reachable as they come
                control = web_hints.hint_disclosure(category["hint"], label=category["label"])
                self.assertNotIn("onclick", control)
                self.assertNotIn("tabindex", control)
        self.assertNotIn("<script", money)
        self.assertNotIn("onclick", money)

    def test_the_screen_reader_reads_the_hint_with_its_category_name(self) -> None:
        for category in self.hinted_categories():
            with self.subTest(category=category["label"]):
                accessible = web_hints.hint_disclosure(category["hint"], label=category["label"])
                self.assertIn(f'aria-label="Подсказка: {category["label"]}"', accessible)
                self.assertIn(category["hint"], accessible)


class AllocationExplanationTests(ServedReleaseTestCase):
    def test_the_explanation_is_built_from_the_real_allocation_dto(self) -> None:
        allocation = self.money_payload["allocation"]
        self.assertTrue(allocation["applied"])
        self.assertEqual(9_000_000, allocation["computed_rent_kopecks"])
        block = web_hints.allocation_block(cast(AllocationDTO, AllocationDTO.from_public_dict(allocation)))
        self.assertIn("Правила распределения подвала", block)
        self.assertIn(web_hints.rub_from_kopecks(allocation["computed_rent_kopecks"]), block)
        self.assertIn(web_hints.rub_from_kopecks(allocation["bank_rent_kopecks"]), block)

    def test_the_explanation_sits_next_to_the_category_totals(self) -> None:
        money = self.pages["/money"]
        categories = money.index('id="ledger-heading"')
        rules = money.index("Правила распределения подвала")
        archive = money.index("Исторические публикации")
        self.assertLess(categories, rules)
        self.assertLess(rules, archive)

    def test_the_allocation_money_uses_the_leaflet_convention(self) -> None:
        money = self.pages["/money"]
        block = web_hints.allocation_block(self.allocation_dto())
        for amount in re.findall(r"\d[\d ]*,\d{2} ₽", block):
            with self.subTest(amount=amount):
                self.assertRegex(amount, r"^\d{1,3}(?: \d{3})*,\d{2} ₽$")
        # the second convention ("90000.00 ₽") must not appear anywhere on the page
        self.assertNotRegex(money, r"\d+\.\d{2} ₽")
        self.assertIn("90 000,00 ₽", money)

    def test_the_explanation_carries_the_leaflet_table_furniture(self) -> None:
        block = web_hints.allocation_block(self.allocation_dto())
        self.assertIn('<div class="table-wrap"><table><caption>', block)
        self.assertIn('<th scope="col">Месяц</th>', block)
        self.assertIn('<th scope="row">', block)
        self.assertIn('<td class="num">', block)

    def test_the_explanation_states_the_rent_and_the_bank_payment_apart(self) -> None:
        money = self.pages["/money"]
        self.assertIn("фактический банковский платёж", money)
        self.assertIn("в итоги не добавляется", money)
        self.assertIn("Расчётная аренда отличается от банковского платежа", money)

    def test_the_explanation_never_publishes_an_allocation_key(self) -> None:
        money = self.pages["/money"]
        keys = [adjustment["category_key"] for adjustment in self.money_payload["allocation"]["adjustments"]]
        self.assertTrue(keys, "the release must carry at least one adjustment")
        for key in keys:
            with self.subTest(key=key):
                self.assertNotIn(key, money)


class NoLeakTests(ServedReleaseTestCase):
    def test_every_page_passes_the_deployed_scan(self) -> None:
        for route in ROUTES:
            text = self.pages[route]
            for pattern in _LEAK_PATTERNS:
                for match in pattern.findall(text):
                    self.fail(f"{route}: private token {match!r} reached the page")
            times = set(_TIME_RE.findall(text))
            self.assertLessEqual(times, ALLOWED_TIMES, f"{route} states another time: {times}")

    def test_no_private_identifier_of_the_build_reaches_a_page(self) -> None:
        private = (
            "cat_rent",
            "cat_utilities",
            "cat_supplies",
            "cat_donations",
            "tx_rent",
            "acc_card",
            "acc_personal",
            "owner-synthetic-1",
            "policy-synthetic-1",
        )
        for route in ROUTES:
            for token in private:
                with self.subTest(route=route, token=token):
                    self.assertNotIn(token, self.pages[route])

    def test_no_internal_bucket_word_reaches_a_page(self) -> None:
        for route in ROUTES:
            text = self.pages[route]
            for bucket in ("external_compensation", "commune_subsidy", "operating_expense", "unclassified"):
                with self.subTest(route=route, bucket=bucket):
                    self.assertNotIn(bucket, text)

    def test_the_content_security_policy_is_the_same_and_still_blocks_scripts(self) -> None:
        for route in ROUTES:
            response = self.site._dispatch(
                {"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": f"period={PERIOD}"}
            )
            headers = dict(response.headers)
            with self.subTest(route=route):
                self.assertEqual(CSP, headers["Content-Security-Policy"])
                self.assertNotIn("script-src", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
                self.assertNotIn("<script", response.text)
                self.assertNotIn("<style", response.text)
                self.assertNotIn('style="', response.text)


class HintStylesheetTests(ServedReleaseTestCase):
    """The disclosure must be visibly a control, focusable and AA-readable."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        response = cls.site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": "/static/site.css"})
        cls.css = response.text

    def test_the_disclosure_has_its_own_styles(self) -> None:
        self.assertIn(".hint-disclosure summary", self.css)
        self.assertIn(".hint-text", self.css)

    def test_the_disclosure_keeps_a_visible_focus_ring(self) -> None:
        # `summary` is in the site's focus-visible rule, so the native control is
        # outlined when it is reached with the keyboard
        focus_rule = next(block for block in self.css.split("}") if "focus-visible" in block)
        self.assertIn("summary:focus-visible", focus_rule)
        self.assertIn("outline: 3px solid var(--plum-2)", focus_rule)

    def test_the_colours_the_disclosure_uses_pass_contrast_aa(self) -> None:
        def token(name: str) -> str:
            match = re.search(rf"--{name}: (#[0-9a-fA-F]{{6}})", self.css)
            assert match, f"the stylesheet has no --{name}"
            return match.group(1)

        for ink in ("plum-2", "ink-2"):
            with self.subTest(ink=ink):
                ratio = _contrast(token(ink), token("sheet"))
                self.assertGreaterEqual(ratio, 4.5, f"--{ink} on --sheet is only {ratio:.2f}:1")


def _relative_luminance(colour: str) -> float:
    raw = colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class HostileHintTests(unittest.TestCase):
    """A hint is owner-written free text; it must never be able to break the page."""

    HOSTILE = '</span><script>alert(1)</script> & "кавычки" <b>жирный</b>'

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        root = Path(cls.tmpdir.name)
        cls.release_dir = root / "release"
        hints = dict(HINTS)
        hints["cat_supplies"] = cls.HOSTILE
        cls.release_id = build_release(cls.release_dir, root / "build.sqlite", hints=hints)
        cls.site = PublicSite(release_dir=cls.release_dir)
        cls.money = cls.site._dispatch(
            {"REQUEST_METHOD": "GET", "PATH_INFO": "/money", "QUERY_STRING": f"period={PERIOD}"}
        ).text

    @classmethod
    def tearDownClass(cls) -> None:
        cls.site.close()
        cls.tmpdir.cleanup()

    def test_the_hostile_hint_is_escaped_and_breaks_nothing(self) -> None:
        self.assertNotIn("<script", self.money)
        self.assertNotIn("</span><script", self.money)
        self.assertNotIn("<b>", self.money)
        self.assertIn(html_module.escape(self.HOSTILE), self.money)
        # the markup around it is still exactly the disclosure it should be
        self.assertIn(
            self.disclosure_markup("Расходники", html_module.escape(self.HOSTILE)), self.money
        )
        self.assertEqual(4, self.money.count('<details class="hint-disclosure"'))

    def test_the_page_around_it_is_still_well_formed(self) -> None:
        self.assertTrue(self.money.lower().startswith("<!doctype html>"))
        self.assertIn("</html>", self.money)
        self.assertEqual(
            self.money.count("<details"), self.money.count("</details>")
        )
        self.assertEqual(
            self.money.count("<table"), self.money.count("</table>")
        )

    @staticmethod
    def disclosure_markup(label: str, escaped_hint: str) -> str:
        e = html_module.escape
        return (
            f'<details class="hint-disclosure"><summary aria-label="Подсказка: {e(label, quote=True)}">'
            f'?</summary><span class="hint-text">{escaped_hint}</span></details>'
        )


class InertAllocationTests(unittest.TestCase):
    """No approved rules — no block; rules applied — the money states stay intact."""

    def money_dto(
        self, metrics: dict, *, completeness: str = "partial", allocation: AllocationDTO | None = None
    ) -> MoneyDTO:
        return MoneyDTO(
            period=PeriodDTO(
                requested=PERIOD, covered_start=date(2025, 7, 1), covered_end=date(2025, 7, 31), complete=False
            ),
            as_of=date(2025, 7, 31),
            release_id="rel_unit",
            freshness="fresh",
            verification="operational",
            completeness=completeness,
            source_labels=("zenmoney",),
            unit="RUB_kopecks",
            metrics=metrics,
            categories=(),
            allocation=allocation if allocation is not None else AllocationDTO(),
        )

    def test_an_absent_or_inert_allocation_renders_nothing(self) -> None:
        self.assertEqual("", web_hints.allocation_block(None))
        self.assertEqual("", web_hints.allocation_block(AllocationDTO()))
        # a DTO that carries counts but is not applied is not an explanation either
        self.assertEqual("", web_hints.allocation_block(AllocationDTO(applied=False, excluded_categories=2)))
        html = render.money_section(
            self.money_dto({"expense_kopecks": 1000, "coverage_is_final": True}), period_label=PERIOD, historical={}
        )
        self.assertNotIn("Правила распределения подвала", html)

    def test_an_applied_allocation_renders_the_block_in_the_money_section(self) -> None:
        allocation = AllocationDTO(
            applied=True,
            rent_applied=True,
            rent_differs=True,
            computed_rent_kopecks=9_000_000,
            bank_rent_kopecks=4_999_000,
            rent_months=(RentMonthDTO("2025-07", 9_000_000, 4_999_000, True, ""),),
        )
        html = render.money_section(
            self.money_dto({"expense_kopecks": 9_000_000, "coverage_is_final": True}, completeness="complete",
                           allocation=allocation),
            period_label=PERIOD,
            historical={},
        )
        self.assertIn("Правила распределения подвала", html)
        self.assertIn("90 000,00 ₽", html)
        self.assertIn("49 990,00 ₽", html)

    def test_the_block_never_turns_an_unallocated_part_into_a_zero(self) -> None:
        allocation = AllocationDTO(applied=True, excluded_categories=6, excluded_operations=1)
        metrics = {
            "expense_kopecks": 0,
            "compensation_kopecks": 0,
            "unclassified_in_kopecks": 123_456,
            "unclassified_out_kopecks": 0,
            "coverage_is_final": False,
        }
        html = render.money_section(
            self.money_dto(metrics, allocation=allocation), period_label=PERIOD, historical={}
        )
        self.assertIn("Правила распределения подвала", html)
        block = web_hints.allocation_block(allocation)
        self.assertIn("Из отчёта исключены категории: 6", block)
        self.assertIn("Из отчёта обратимо исключены отдельные операции: 1", block)
        # the unallocated part is still stated as an amount, and the block itself
        # never dresses it as a classified zero
        self.assertIn("Неразнесённые суммы: приход", html)
        self.assertIn("1 234,56 ₽", html)
        self.assertNotIn("0,00 ₽", block)

    def test_the_two_money_renderers_agree(self) -> None:
        for kopecks in (None, 0, 1, 999, 1_000, 100_000, 6_508_900, 9_000_000, -4_558_900, 10**12):
            with self.subTest(kopecks=kopecks):
                self.assertEqual(render.rub_from_kopecks(kopecks), web_hints.rub_from_kopecks(kopecks))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
