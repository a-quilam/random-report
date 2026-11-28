"""Owner remarks 2-7 on the approved home sheet (branch ``revision/home-details``).

The owner compared the live sheet with the approved prototype and listed what the
fragment could not answer on its own. This module pins the *new* behaviour, per
remark:

* (2) every ledger row carries its **own** hint: the approved ``CategoryDTO.hint``
  when the owner wrote one, otherwise a short neutral phrase from the confirmed
  label; the grouped «Прочее» row lists the articles that make up the sum. One
  identical text on all rows was reported as a defect, and an invented number is
  worse.
* (3) the first row's hint must not be clipped and must not be covered: the hint is
  the owner-approved hover plate (``.helpful:after``), and the torn edge of
  ``.source-sheet`` moved onto its own pseudo-elements, so the container no longer
  clips the plate that the first row floats above itself.
* (4)/(4.1) the small line under the subscriber count — and the past-month line —
  carry a keyboard-reachable hover plate (``tabindex`` + ``data-help``); an exact
  30-day base is reported as a real change, otherwise the honest dated wording,
  never a fabricated zero.
* (5)/(6) the finance plate prints the short month name; «предварительно» and the
  snapshot date live in the hover plate of the caption; the note is the short
  «НЕ ПОКРЫТО: N ₽» from the public DTO, and an unknown value reads «нет данных»,
  not zero.
* (7) the asterisk by the title is replaced by the owner's logo: a compact public
  PNG without ancillary metadata, rendered with an ``alt`` text.
"""

from __future__ import annotations

import re
import struct
import tempfile
import unittest
from pathlib import Path

from randreport.web import render
from randreport.web.app import PublicSite
from randreport.web.approved_home import page
from tests import dev_release

PERIOD = "2025-07"
STATIC = Path(__file__).resolve().parents[1] / "randreport" / "web" / "static"


def _png_chunk_types(data: bytes) -> set[bytes]:
    """The 4-byte chunk type of every PNG chunk (``IHDR``, ``tEXt``, …)."""

    types: set[bytes] = set()
    pos = 8  # skip the 8-byte signature
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        types.add(data[pos + 4 : pos + 8])
        pos += 12 + length  # length + type + data + CRC
    return types


class _ServedHome:
    """The home page through the real WSGI callable, on a synthetic release."""

    def __enter__(self) -> str:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        dev_release.build(str(root / "release"), period=PERIOD, db_path=str(root / "build.sqlite"))
        site = PublicSite(release_dir=root / "release")
        try:
            return site._dispatch(
                {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": f"period={PERIOD}"}
            ).text
        finally:
            site.close()

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


class TestTheOwnerLogoReplacesTheAsterisk(unittest.TestCase):
    """Remark 7: the owner's logo, a compact public PNG with no metadata."""

    def test_the_home_brand_renders_the_logo_with_alt_text(self) -> None:
        with _ServedHome() as html:
            brand = re.search(r'<a class="brand".*?</a>', html, re.S).group(0)
        self.assertIn('<img class="brand-mark" src="/static/brand-logo.png"', brand)
        self.assertIn('alt="РанДом Культуры"', brand)
        self.assertNotIn('class="star"', brand)

    def test_the_asset_is_a_png_without_ancillary_metadata(self) -> None:
        asset = STATIC / render.BRAND_LOGO
        self.assertTrue(asset.is_file(), "the logo asset must ship in the package static dir")
        chunks = _png_chunk_types(asset.read_bytes())
        for forbidden in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"):
            self.assertNotIn(forbidden, chunks, f"the public logo still carries {forbidden!r}")
        self.assertLess(asset.stat().st_size, 120_000, "the public logo must stay compact")

    def test_without_the_asset_the_decorative_mark_returns(self) -> None:
        # The operator may point the static contour elsewhere; a missing logo must
        # leave the decorative mark rather than a broken image.
        html = page(
            period_label="2026-11", money=None, events=None, historical={},
            community=None, logo_url=None,
        )
        self.assertIn('<span class="star" aria-hidden="true">✳</span>', html)
        self.assertNotIn("brand-mark", html)


class TestSublineHintsAreReachable(unittest.TestCase):
    """Remarks 4 and 4.1: honest small print behind a keyboard-reachable plate."""

    def test_the_two_sublines_carry_their_own_hover_hints(self) -> None:
        with _ServedHome() as html:
            plates = re.findall(
                r'<span class="subline helpful" tabindex="0" id="(\w+)" data-help="(.*?)">(.*?)</span>',
                html, re.S)
        self.assertEqual(2, len(plates))
        (past_id, past_help, past_text), (growth_id, growth_help, growth_text) = plates
        self.assertEqual('prevEvents', past_id)
        self.assertEqual('subGrowth', growth_id)
        # the value under the figure stays the visible text of the line
        self.assertIn("июнь", past_text)
        # each hint is distinct — not the same text copied onto both lines
        self.assertNotEqual(past_help, growth_help)
        self.assertIn("30", growth_help)
        # the hint is a plate, not a click disclosure
        self.assertNotIn('<details class="hint subline-hint">', html)

    def test_a_change_is_only_claimed_for_an_exact_thirty_day_base(self) -> None:
        from datetime import date
        from types import SimpleNamespace as Point

        def text(day: int) -> str:
            end = Point(on=date(2026, 11, 23), subscribers=105)
            start = Point(on=date(2026, 10, day), subscribers=100)
            return page(
                period_label="2026-11",
                money=None,
                events=None,
                historical={},
                community=Point(
                    metrics={"latest_subscribers": 105, "latest_on": "2026-11-23"},
                    channel_points=(end,),
                ),
                subscribers_extra=(start,),
            )

        self.assertIn("+5 за 30 дней", text(24))
        self.assertIn("Нет сравнения за 30 дней", text(23))

    def test_an_unknown_change_is_not_dressed_as_a_zero(self) -> None:
        html = page(period_label="2026-11", money=None, events=None, historical={}, community=None)
        self.assertIn("Нет сравнения за 30 дней", html)
        self.assertNotIn("+0", html)


class TestHintAndSmallPrintStyling(unittest.TestCase):
    """Remark 3: the approved hover plate, unclipped, with a visible focus ring."""

    def setUp(self) -> None:
        self.css = (STATIC / "approved-home.css").read_text(encoding="utf-8")

    def test_the_hint_is_the_approved_hover_plate(self) -> None:
        # the rule is the prototype's own one, with :focus so a tap opens it too
        self.assertIn(".helpful{position:relative}", self.css)
        self.assertIn(".helpful:hover:after", self.css)
        self.assertIn(".helpful:focus-visible:after", self.css)
        self.assertIn(".helpful:focus:after", self.css)
        self.assertIn("content:attr(data-help)", self.css)
        self.assertIn("background:#24201c", self.css)
        self.assertIn("box-shadow:4px 4px 0 #c23e34", self.css)
        self.assertIn("pointer-events:none", self.css)
        self.assertIn(".helpful:focus-visible{outline:3px solid var(--darkpurple)", self.css)
        # no part of the sheet is a click disclosure any more
        self.assertNotIn(".tip", self.css)
        self.assertNotIn("hint>summary", self.css)

    def test_the_sheet_no_longer_clips_its_children(self) -> None:
        # the torn paper edge belongs to the pseudo-elements; the container itself
        # must not carry a clip-path, or the first row's plate is cut off.
        rule = re.search(r'\.source-sheet\{[^}]*\}', self.css).group(0)
        self.assertNotIn("clip-path", rule)
        self.assertIn("isolation:isolate", rule)
        for pseudo in (r'\.source-sheet:before\{', r'\.source-sheet:after\{'):
            edge = re.search(pseudo + r'[^}]*\}', self.css).group(0)
            self.assertIn("clip-path:polygon(0 1%", edge)
            self.assertIn("pointer-events:none", edge)

    def test_the_small_line_and_the_logo_are_pinned_in_the_stylesheet(self) -> None:
        self.assertIn(".helpful{position:relative}", self.css)
        rules = re.findall(r'\.brand \.brand-mark\{[^}]*\}', self.css)
        self.assertTrue(rules, "the mark must be pinned by class, not by inline style")
        # the base rule scales the owner's mark to the field left of the name plate,
        # on its own layer above the paper
        self.assertIn("width:72px", rules[0])
        self.assertIn("z-index:6", rules[0])
        # The owner moved the angle in steps on this same card (16° -> +40 deasil ->
        # -80°, and now 80° deasil *from the shown* -80°, i.e. level with the asset
        # again) and then slid the mark a few px left. The last word in the
        # stylesheet wins, so the alignment block is appended and carries the
        # effective values; the superseded ones must not come back.
        marker = "/* Optical alignment"
        self.assertIn(marker, self.css)
        tail = self.css.split(marker, 1)[1]
        self.assertIn("transform:rotate(0deg)", tail)
        self.assertIn("left:-56px", tail)
        self.assertIn("left:-24px", tail)  # the same slide at <=800px
        self.assertNotIn("rotate(-80deg)", tail)
        self.assertNotIn("rotate(40deg)", tail)
        # the figures sit a few px higher inside their own paper; the cutout and the
        # values themselves stay where they were
        for rule in ("#eventsCount:not(.wide){padding-top:5px",
                     "#subCount:not(.wide){padding-top:6px",
                     "#percent{position:relative;top:-3px}"):
            self.assertIn(rule, self.css)

    def test_the_plate_of_a_right_hand_hint_stays_on_the_sheet_when_narrow(self) -> None:
        # The plate is measured from the trigger's own left edge (``left:5%``) and is
        # at least 180px wide. At <=800px the coverage caption and the subscriber
        # subline sit in the right-hand column, so the plate ran past the torn edge of
        # ``.poster{overflow:hidden}`` and its text read clipped on 390/320 — measured
        # in a live browser, not in the markup. The mobile block anchors the right edge
        # of those two plates to the right edge of their own row; the hints of the left
        # column and the ledger rows keep the prototype's offset.
        block = self.css.rsplit("@media(max-width:800px)", 1)[1]
        caption = re.search(r'\.finance \.fin-caption\.helpful:hover:after[^}]*\}', block).group(0)
        self.assertIn("left:auto", caption)
        self.assertIn("right:0", caption)
        self.assertIn(".metric.subs .helpful:focus-visible:after", block)
        self.assertNotIn(".metric.events", block)
        self.assertNotIn(".source-row", block)
        # the prototype's own offset survives for every other hint
        self.assertIn("left:5%", self.css)

    def test_every_home_hint_is_a_plate_with_its_own_text(self) -> None:
        with _ServedHome() as html:
            # no hint on the sheet opens on click any more
            self.assertNotIn('<details class="hint', html)
            triggers = re.findall(r'class="[^"]*helpful[^"]*"[^>]*', html)
            # three ledger rows, two sublines and the finance caption
            self.assertEqual(6, len(triggers))
            for trigger in triggers:
                self.assertIn('data-help="', trigger)
                self.assertIn('tabindex="0"', trigger)
            texts = re.findall(r'data-help="([^"]*)"', html)
            self.assertEqual(len(texts), len(set(texts)), 'every plate carries its own text')

    def test_no_inline_style_reaches_the_home_sheet(self) -> None:
        with _ServedHome() as html:
            self.assertNotIn("style=\"", html)
            self.assertNotIn("<style", html)
            self.assertNotIn("<script", html)


if __name__ == "__main__":
    unittest.main()
