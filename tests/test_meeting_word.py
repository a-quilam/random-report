"""Meeting-title rule: matched privately, never published.

The owner's decision is that a record of the allowed closed contour is an assembly
when its title satisfies the configured meeting rule. This test pins *both*
recorded forms: the conservative default ``exact`` (the whole title must be
«Собрания»; ``Общее собрание коммуны`` or ``Итоги: СОБРАНИЯ!`` are **not**
meetings, because promoting them would put a private record's date on the public
calendar) and the owner's later recorded variant ``contains``
(``source_title_contains_word`` with ``case_sensitive: false`` in
``закрытый файл правил владельца``), which only ever applies when it is configured explicitly.
Conflict/uncertainty is resolved conservatively — fewer records, never more.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.domain.meeting_title import (  # noqa: E402
    DEFAULT_MEETING_MATCH,
    DEFAULT_MEETING_TITLES,
    MATCH_CONTAINS,
    MATCH_EXACT,
    MeetingMatchError,
    is_meeting_title,
    looks_like_meeting_title,
    matches_meeting_title,
)


class MeetingTitleTests(unittest.TestCase):
    def test_exact_title_is_a_meeting(self):
        for title in ("Собрания",):
            with self.subTest(title=title):
                self.assertTrue(is_meeting_title(title))

    def test_word_inside_a_longer_title_is_not_a_meeting(self):
        for title in ("Общее собрание коммуны", "Итоги: СОБРАНИЯ!", "«Собрание»", "Собрания жильцов", " Собрания"):
            with self.subTest(title=title):
                self.assertFalse(is_meeting_title(title))
                self.assertTrue(looks_like_meeting_title(title))

    def test_a_word_variant_is_flagged_but_is_not_a_meeting(self):
        for title in ("предсобрание", "собраниями"):
            with self.subTest(title=title):
                self.assertFalse(is_meeting_title(title))
                self.assertTrue(looks_like_meeting_title(title))

    def test_unrelated_private_title_is_not_a_meeting(self):
        for title in ("Аренда", "", None):
            with self.subTest(title=title):
                self.assertFalse(is_meeting_title(title))
                self.assertFalse(looks_like_meeting_title(title))

    def test_the_rule_is_configurable_without_real_values(self):
        self.assertEqual(frozenset({"Собрания"}), DEFAULT_MEETING_TITLES)
        self.assertTrue(is_meeting_title("Собрание", {"Собрание"}))
        self.assertFalse(is_meeting_title("Собрания", {"Собрание"}))

    def test_the_default_mode_is_the_conservative_exact_match(self):
        self.assertEqual(MATCH_EXACT, DEFAULT_MEETING_MATCH)
        self.assertFalse(matches_meeting_title("Общее собрание коммуны"))
        self.assertFalse(matches_meeting_title("Собрания жильцов"))
        self.assertTrue(matches_meeting_title("Собрания"))
        # the mode is explicit data, never inferred from the titles
        self.assertFalse(matches_meeting_title("Общее собрание коммуны", mode=MATCH_EXACT))

    def test_the_owner_recorded_contains_mode_matches_a_substring_case_insensitively(self):
        """закрытый файл правил владельца: ``source_title_contains_word`` + ``case_sensitive: false``."""

        words = frozenset({"собрание", "собрания"})
        for title in ("Общее собрание коммуны", "Итоги: СОБРАНИЯ!", "Собрания", "собрание жильцов", "СОБРАНИЕ:"):
            with self.subTest(title=title):
                self.assertTrue(matches_meeting_title(title, words, mode=MATCH_CONTAINS))
        # the recorded rule is literally "the title *contains* the word", so the
        # word counts inside a longer or glued name too (the merged-name case)
        for title in ("Коммусобрание", "предсобрание", "собраниями", "КураторскоеСобрание2025"):
            with self.subTest(title=title):
                self.assertTrue(matches_meeting_title(title, words, mode=MATCH_CONTAINS))
        # an unrelated title and the empty cases are never a meeting
        for title in ("Аренда зала", "", None):
            with self.subTest(title=title):
                self.assertFalse(matches_meeting_title(title, words, mode=MATCH_CONTAINS))

    def test_an_unknown_mode_is_refused_never_treated_as_permissive(self):
        with self.assertRaises(MeetingMatchError):
            matches_meeting_title("Собрания", mode="substring")
        with self.assertRaises(MeetingMatchError):
            matches_meeting_title("Собрания", mode="")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
