"""The meeting-title rule: matched privately, never published.

A record of the *allowed closed contour* (``EventTypeMapping.meeting_subcalendars``)
is an assembly only when its title matches the configured meeting titles.  Two
matching modes exist, because the owner's rule has been recorded in two forms and
the more conservative one is the default:

* ``exact`` (default) — the title must be exactly one of the configured titles
  («Собрания» by default). A longer title that merely contains the word
  (``Общее собрание коммуны``, ``Итоги: СОБРАНИЯ!``) is **not** a meeting:
  promoting it would put a private record's date on the public calendar, so the
  uncertain case is resolved conservatively — fewer records, never more.

* ``contains`` — the title must contain one of the configured words *as a whole
  word*, compared case-insensitively (the owner's recorded rule in
  the recorded rule: ``source_title_contains_word`` with ``case_sensitive:
  false``).  This is *less* conservative on purpose and is only active when it is
  configured explicitly; it is never the default.

The raw title is used only to classify; the public label is a constant
(``EventTypeMapping.meeting_label``), so the source title never leaves the domain.
A near miss (a title that contains a meeting title but does not match under the
active mode) is surfaced to admins instead of being guessed away.
"""

from __future__ import annotations

from typing import AbstractSet

DEFAULT_MEETING_TITLE = "Собрания"

DEFAULT_MEETING_TITLES: frozenset[str] = frozenset({DEFAULT_MEETING_TITLE})

#: The conservative default: the whole title must match.
MATCH_EXACT = "exact"
#: The owner's recorded variant: the title must contain one of the words.
MATCH_CONTAINS = "contains"
MEETING_MATCH_MODES: frozenset[str] = frozenset({MATCH_EXACT, MATCH_CONTAINS})
DEFAULT_MEETING_MATCH = MATCH_EXACT


class MeetingMatchError(ValueError):
    """A meeting-match mode that cannot be trusted."""


def is_meeting_title(title: str | None, titles: AbstractSet[str] = DEFAULT_MEETING_TITLES) -> bool:
    """True only for an *exact* match against a configured meeting title."""

    return title is not None and title in titles


def _contains_word(title: str, word: str) -> bool:
    """True when ``word`` occurs in ``title`` as a whole word.

    ``str.isalpha`` decides the boundary rather than ``re``'s ``\\b``, which does
    not treat Cyrillic letters as word characters.
    """

    start = 0
    while True:
        index = title.find(word, start)
        if index < 0:
            return False
        before = title[index - 1] if index else ""
        after_index = index + len(word)
        after = title[after_index : after_index + 1]
        if not before.isalpha() and not after.isalpha():
            return True
        start = index + 1


def matches_meeting_title(
    title: str | None,
    titles: AbstractSet[str] = DEFAULT_MEETING_TITLES,
    *,
    mode: str = DEFAULT_MEETING_MATCH,
) -> bool:
    """Match ``title`` against the configured titles under the active mode.

    An unknown mode is refused rather than silently treated as the permissive
    one: a typo in the configuration must never widen what reaches the public
    calendar.
    """

    if mode not in MEETING_MATCH_MODES:
        raise MeetingMatchError(f"unknown meeting match mode {mode!r}")
    if title is None:
        return False
    if mode == MATCH_EXACT:
        return title in titles
    lowered = title.casefold()
    words = [candidate.casefold() for candidate in titles if candidate]
    return any(_contains_word(lowered, word) for word in words)


def looks_like_meeting_title(title: str | None, titles: AbstractSet[str] = DEFAULT_MEETING_TITLES) -> bool:
    """True when a title merely *contains* a meeting title (a near miss).

    The word stem is enough (``Собрания`` → ``собрани``), so ``Общее собрание``,
    ``предсобрание`` and ``собраниями`` are all surfaced to admins — under the
    default ``exact`` mode they are never classified as a meeting, but a mistyped
    or extended title may be a real assembly.
    """

    if not title or not titles:
        return False
    needles: set[str] = set()
    for candidate in titles:
        if not candidate:
            continue
        folded = candidate.casefold()
        needles.add(folded)
        if len(folded) > 3:
            needles.add(folded[:-1])
    lower = title.casefold()
    contained = any(needle in lower for needle in needles)
    return contained and not is_meeting_title(title, titles)


__all__ = [
    "DEFAULT_MEETING_MATCH",
    "DEFAULT_MEETING_TITLE",
    "DEFAULT_MEETING_TITLES",
    "MATCH_CONTAINS",
    "MATCH_EXACT",
    "MEETING_MATCH_MODES",
    "MeetingMatchError",
    "is_meeting_title",
    "looks_like_meeting_title",
    "matches_meeting_title",
]
