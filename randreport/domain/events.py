"""Calendar rules: which occurrences actually happened and what may be published.

Completed means: the record exists in the calendar (or in the local archive after
source retention) at the moment of a successful sync and its end is at or before
the run cutoff. No mandatory organiser confirmation, no attendance, no LLM
selection.

The closed contour is configured by *stable subcalendar IDs* (never by a title or
a colour alone). The owner decided that a completed record of the **allowed closed
contour** whose title matches a configured meeting title becomes an assembly
(``meeting``): it is counted once and, in the public calendar, appears only as its
Moscow start date plus the constant label «Собрание» — never its raw title, id,
time, url, description or people.  The match mode is configuration: ``exact``
(the conservative default, the whole title must equal a configured title) or
``contains`` (the owner's recorded variant: the title must
contain a configured word, case-insensitive). Other closed records stay
aggregate-only, split into a separate rent category by mapping (not every closed
record is a rent). A record in an *excluded* subcalendar contributes nothing at
all — neither lists nor totals. Conflict and uncertainty are resolved
conservatively (most restrictive wins).

See docs/REQUIREMENTS.md sections 3.3, 7, 13 (E01-E03), docs/DOMAIN.md and
docs/CALENDAR.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import AbstractSet, Iterable, Mapping, Sequence

from .meeting_title import (
    DEFAULT_MEETING_MATCH,
    DEFAULT_MEETING_TITLE,
    DEFAULT_MEETING_TITLES,
    MEETING_MATCH_MODES,
    MeetingMatchError,
    looks_like_meeting_title,
    matches_meeting_title,
)
from .money import share_percent
from .timeframes import MSK, msk_day


class EventType(StrEnum):
    OPEN = "open"
    OPEN_BY_REGISTRATION = "open_by_registration"
    CLOSED = "closed"
    INTERNAL_MEETING = "internal_meeting"
    PREPARATION = "preparation"
    UNKNOWN = "unknown"
    # Derived public categories of the closed contour (never assigned directly to a
    # source subcalendar; they refine a base ``closed`` record by configuration):
    MEETING = "meeting"
    CLOSED_RENT = "closed_rent"


TRADITIONAL_TYPES = (
    EventType.OPEN,
    EventType.OPEN_BY_REGISTRATION,
    EventType.CLOSED,
    EventType.INTERNAL_MEETING,
    EventType.PREPARATION,
)
# Types a subcalendar may be mapped to by hand; the rest are derived at classification.
SOURCE_TYPES = TRADITIONAL_TYPES + (EventType.UNKNOWN,)
DERIVED_TYPES = (EventType.MEETING, EventType.CLOSED_RENT)
ALL_TYPES = TRADITIONAL_TYPES + DERIVED_TYPES + (EventType.UNKNOWN,)
PUBLIC_TYPES = frozenset({EventType.OPEN, EventType.OPEN_BY_REGISTRATION})
PRIVATE_TYPES = frozenset({EventType.CLOSED, EventType.INTERNAL_MEETING})

# Most restrictive first: a record listed in several subcalendars is classified
# by the most restrictive of them.
TYPE_PRIORITY = (
    EventType.CLOSED,
    EventType.INTERNAL_MEETING,
    EventType.PREPARATION,
    EventType.OPEN_BY_REGISTRATION,
    EventType.OPEN,
    EventType.UNKNOWN,
)

CANCELLATION_MARKERS = ("отмен", "cancel")

#: The revision of a record whose source did not send one. A source revision is
#: an *opaque token* (Teamup sends a hex string): it is never parsed into a
#: number, never read as a time and never compared - an opaque token orders
#: nothing. The default therefore is a documented token, not ``1``.
UNVERSIONED_REVISION = "unversioned"

DEFAULT_MEETING_LABEL = "Собрание"

# Environment surface for the stable-ID configuration (placeholders only in .env.example).
ENV_SUBCALENDAR_TYPES = "TEAMUP_SUBCALENDAR_TYPES"
ENV_PRIVATE_SUBCALENDARS = "TEAMUP_PRIVATE_SUBCALENDARS"
ENV_EXCLUDED_SUBCALENDARS = "TEAMUP_EXCLUDED_SUBCALENDARS"
ENV_MEETING_SUBCALENDARS = "TEAMUP_MEETING_SUBCALENDARS"
ENV_RENT_SUBCALENDARS = "TEAMUP_RENT_SUBCALENDARS"
ENV_MEETING_TITLES = "TEAMUP_MEETING_TITLES"
ENV_MEETING_LABEL = "TEAMUP_MEETING_LABEL"
ENV_MEETING_MATCH = "TEAMUP_MEETING_MATCH"


class EventError(ValueError):
    """An occurrence or mapping that cannot be trusted."""


@dataclass(frozen=True)
class Occurrence:
    calendar_id: str
    occurrence_id: str
    start: datetime
    end: datetime | None
    subcalendar_ids: tuple[str, ...] = ()
    series_id: str | None = None
    title: str | None = None
    all_day: bool = False
    raw_type: str | None = None
    revision: str = UNVERSIONED_REVISION
    deleted: bool = False
    source_present: bool = True
    local_archive: bool = False

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise EventError("occurrence start must be timezone aware")
        if self.end is not None and self.end.tzinfo is None:
            raise EventError("occurrence end must be timezone aware")
        if self.end is not None and self.end < self.start:
            raise EventError("occurrence end is before its start")

    @property
    def key(self) -> tuple[str, str]:
        return (self.calendar_id, self.occurrence_id)

    def day(self) -> date:
        """Moscow start day: the period of an event is decided by its start."""

        return msk_day(self.start)

    def is_completed(self, cutoff: datetime) -> bool:
        """Finished at or before the cutoff; an unknown end is never assumed."""

        return self.end is not None and self.end <= cutoff


@dataclass(frozen=True)
class EventTypeMapping:
    """Versioned, stable-ID mapping of the calendar (never a title or a colour).

    ``subcalendar_types`` gives the base type of a subcalendar; ``private_subcalendars``
    forces aggregate-only publication. The remaining sets refine the closed contour:

    * ``meeting_subcalendars`` — the *allowed contour*: only a record here can be a
      ``meeting`` (and only on an exact title match);
    * ``rent_subcalendars`` — closed records mapped as rents (a separate public
      category), so not every closed record is counted as a rent;
    * ``excluded_subcalendars`` — records that are dropped entirely: no lists, no totals.
    """

    version: str = "mapping"
    subcalendar_types: Mapping[str, EventType] = field(default_factory=dict)
    private_subcalendars: frozenset[str] = frozenset()
    excluded_subcalendars: frozenset[str] = frozenset()
    meeting_subcalendars: frozenset[str] = frozenset()
    rent_subcalendars: frozenset[str] = frozenset()
    meeting_titles: frozenset[str] = DEFAULT_MEETING_TITLES
    meeting_label: str = DEFAULT_MEETING_LABEL
    #: ``exact`` (default, conservative) or ``contains`` (the owner's recorded
    #: variant: the title must contain one of ``meeting_titles``, case-insensitive)
    meeting_match: str = DEFAULT_MEETING_MATCH

    def event_type(self, subcalendar_ids: Sequence[str]) -> EventType:
        present = {self.subcalendar_types.get(sid, EventType.UNKNOWN) for sid in subcalendar_ids}
        for candidate in TYPE_PRIORITY:
            if candidate in present:
                return candidate
        return EventType.UNKNOWN

    def is_private(self, subcalendar_ids: Sequence[str]) -> bool:
        if any(sid in self.private_subcalendars for sid in subcalendar_ids):
            return True
        return self.event_type(subcalendar_ids) in PRIVATE_TYPES

    def is_excluded(self, subcalendar_ids: Sequence[str]) -> bool:
        """Excluded wins over everything: the record is not listed and not counted."""

        return any(sid in self.excluded_subcalendars for sid in subcalendar_ids)

    def in_meeting_contour(self, subcalendar_ids: Sequence[str]) -> bool:
        return any(sid in self.meeting_subcalendars for sid in subcalendar_ids)

    def closed_kind(self, subcalendar_ids: Sequence[str], title: str | None) -> EventType:
        """Refine a base ``closed`` record into a meeting, a rent, or other closed.

        Matching is done under ``meeting_match``. The default is ``exact``: a
        longer title that merely contains the word is **not** a meeting
        (conservative — fewer records get a public date); ``contains`` is the
        owner's explicitly recorded variant and is only used when configured. A
        record that is mapped both into the meeting contour and as a rent is a
        conflicting configuration: the conservative reading wins, so it is
        published aggregate-only as a rent, never as a public meeting.
        """

        in_meeting_contour = self.in_meeting_contour(subcalendar_ids)
        in_rent = any(sid in self.rent_subcalendars for sid in subcalendar_ids)
        if in_meeting_contour and in_rent:
            in_meeting_contour = False
        if in_meeting_contour and matches_meeting_title(title, self.meeting_titles, mode=self.meeting_match):
            return EventType.MEETING
        if in_rent:
            return EventType.CLOSED_RENT
        return EventType.CLOSED


@dataclass(frozen=True)
class ClassifiedOccurrence:
    occurrence: Occurrence
    event_type: EventType
    public: bool
    reason: str
    #: the constant public label of a meeting (never the source title); ``None`` otherwise
    public_label: str | None = None


@dataclass(frozen=True)
class EventSelection:
    cutoff: datetime
    completed: tuple[ClassifiedOccurrence, ...]
    ongoing: tuple[Occurrence, ...]
    future: tuple[Occurrence, ...]
    deleted: tuple[Occurrence, ...]
    unknown_end: tuple[Occurrence, ...]
    superseded: tuple[Occurrence, ...]
    local_archive_only: tuple[Occurrence, ...]
    duplicates_collapsed: int
    revision_log: tuple[Occurrence, ...]
    admin_flags: tuple[str, ...]
    #: records dropped by an excluded subcalendar — internal only, never published
    excluded: tuple[Occurrence, ...] = ()


def _mentions_cancellation(title: str | None) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(marker in lowered for marker in CANCELLATION_MARKERS)


def select_completed(
    occurrences: Iterable[Occurrence],
    *,
    mapping: EventTypeMapping,
    cutoff: datetime,
) -> EventSelection:
    """Split occurrences into completed / ongoing / future / unknown and keep revisions."""

    if cutoff.tzinfo is None:
        raise EventError("cutoff must be timezone aware")

    ordered = sorted(occurrences, key=lambda occ: occ.key)
    revision_log = tuple(ordered)

    # An opaque revision orders nothing, so the resolutions never compare two
    # revision tokens: the *last delivery* of a key is the source's current state
    # of that occurrence (the store keeps one row per key in the same order), and
    # an earlier delivery of the same key is logged and dropped from the counts.
    latest: dict[tuple[str, str], Occurrence] = {}
    superseded: list[Occurrence] = []
    for occurrence in ordered:
        current = latest.get(occurrence.key)
        if current is not None:
            superseded.append(current)
        latest[occurrence.key] = occurrence

    completed: list[ClassifiedOccurrence] = []
    ongoing: list[Occurrence] = []
    future: list[Occurrence] = []
    deleted: list[Occurrence] = []
    unknown_end: list[Occurrence] = []
    local_archive_only: list[Occurrence] = []
    excluded: list[Occurrence] = []
    admin_flags: list[str] = []

    for occurrence in latest.values():
        if occurrence.deleted:
            deleted.append(occurrence)
            continue
        if mapping.is_excluded(occurrence.subcalendar_ids):
            # an excluded subcalendar yields no list and no total (even as a zero)
            excluded.append(occurrence)
            continue
        base_type = mapping.event_type(occurrence.subcalendar_ids)
        event_type = base_type
        if base_type == EventType.CLOSED:
            event_type = mapping.closed_kind(occurrence.subcalendar_ids, occurrence.title)
        # published with date and title only for genuinely public formats; closed,
        # internal, preparation and unknown formats stay aggregate-only, and a
        # meeting is published as its date plus the constant label only
        private = mapping.is_private(occurrence.subcalendar_ids)
        public = base_type in PUBLIC_TYPES and not private
        if _mentions_cancellation(occurrence.title):
            # the source mapping wins; a possible problem is surfaced to admins, never guessed away
            admin_flags.append(occurrence.occurrence_id)
        if (
            base_type == EventType.CLOSED
            and event_type != EventType.MEETING
            and mapping.in_meeting_contour(occurrence.subcalendar_ids)
            and looks_like_meeting_title(occurrence.title, mapping.meeting_titles)
        ):
            # a near miss inside the meeting contour: shown to admins, never published
            admin_flags.append(occurrence.occurrence_id)
        if (
            base_type == EventType.CLOSED
            and mapping.in_meeting_contour(occurrence.subcalendar_ids)
            and any(sid in mapping.rent_subcalendars for sid in occurrence.subcalendar_ids)
        ):
            # a conflicting configuration (meeting contour ∩ rent): resolved conservatively,
            # so the record is aggregate-only; the conflict is surfaced to admins
            admin_flags.append(occurrence.occurrence_id)
        classified = ClassifiedOccurrence(
            occurrence=occurrence,
            event_type=event_type,
            public=public,
            reason="subcalendar_mapping" if occurrence.subcalendar_ids else "no_subcalendar",
            public_label=mapping.meeting_label if event_type == EventType.MEETING else None,
        )
        if occurrence.end is None:
            unknown_end.append(occurrence)
            continue
        if occurrence.end <= cutoff:
            completed.append(classified)
            if occurrence.local_archive:
                local_archive_only.append(occurrence)
        elif occurrence.start <= cutoff:
            ongoing.append(occurrence)
        else:
            future.append(occurrence)

    completed.sort(key=lambda item: (item.occurrence.start, item.occurrence.occurrence_id))
    return EventSelection(
        cutoff=cutoff,
        completed=tuple(completed),
        ongoing=tuple(ongoing),
        future=tuple(future),
        deleted=tuple(deleted),
        unknown_end=tuple(unknown_end),
        superseded=tuple(superseded),
        local_archive_only=tuple(local_archive_only),
        duplicates_collapsed=len(superseded),
        revision_log=revision_log,
        admin_flags=tuple(admin_flags),
        excluded=tuple(excluded),
    )


@dataclass(frozen=True)
class EventStats:
    cutoff: datetime
    total: int
    by_type: Mapping[str, int]
    public_total: int
    public_share_percent: Decimal | None
    closed_total: int
    local_archive_total: int
    meeting_total: int = 0
    closed_rent_total: int = 0

    def public_payload(self) -> dict:
        """Aggregate-only view: type counts and totals, never a closed detail."""

        return {
            "total_events": self.total,
            "event_type_aggregates": dict(self.by_type),
            "public_events": self.public_total,
            "closed_aggregate": self.closed_total,
            "closed_rent_aggregate": self.closed_rent_total,
            "meeting_aggregate": self.meeting_total,
            "unknown_type": self.by_type[EventType.UNKNOWN.value],
            "public_share_percent": None if self.public_share_percent is None else str(self.public_share_percent),
            "local_archive_events": self.local_archive_total,
        }


def event_stats(selection: EventSelection, *, cutoff: datetime | None = None) -> EventStats:
    """Count completed occurrences by type; every type is present, zeros included."""

    by_type = {event_type.value: 0 for event_type in ALL_TYPES}
    for classified in selection.completed:
        by_type[classified.event_type.value] += 1
    total = len(selection.completed)
    public_total = sum(1 for c in selection.completed if c.public)
    closed_total = by_type[EventType.CLOSED.value] + by_type[EventType.CLOSED_RENT.value]
    return EventStats(
        cutoff=cutoff or selection.cutoff,
        total=total,
        by_type=by_type,
        public_total=public_total,
        public_share_percent=share_percent(public_total, total) if total else None,
        closed_total=closed_total,
        local_archive_total=len(selection.local_archive_only),
        meeting_total=by_type[EventType.MEETING.value],
        closed_rent_total=by_type[EventType.CLOSED_RENT.value],
    )


def public_calendar_days(selection: EventSelection) -> Mapping[str, int]:
    """Moscow days with completed *public* events only.

    Closed/internal/meeting days are absent on purpose: a zero would let anyone
    recover a closed date by subtracting the totals from a public calendar.
    """

    days: dict[str, int] = {}
    for classified in selection.completed:
        if not classified.public:
            continue
        key = classified.occurrence.day().isoformat()
        days[key] = days.get(key, 0) + 1
    return dict(sorted(days.items()))


@dataclass(frozen=True)
class PublicEvent:
    """A publishable event: no source ids, no calendar ids, no participants."""

    on: date
    type: str
    title: str
    description: str | None = None
    announce_url: str | None = None
    registration_open: bool = False


def public_events(selection: EventSelection) -> tuple[PublicEvent, ...]:
    events: list[PublicEvent] = []
    for classified in selection.completed:
        if not classified.public:
            continue
        occurrence = classified.occurrence
        events.append(
            PublicEvent(
                on=occurrence.day(),
                type=classified.event_type.value,
                title=(occurrence.title or "событие").strip(),
                description=None,
                announce_url=None,
                # a registration form of an already finished event is never current
                registration_open=False,
            )
        )
    return tuple(events)


@dataclass(frozen=True)
class PublicMeeting:
    """A meeting published as its Moscow start day plus the *constant* label.

    No raw title, no id, no time, no url, no description, no people — by construction
    the object has nowhere to carry them.
    """

    on: date
    label: str


def public_meetings(selection: EventSelection) -> tuple[PublicMeeting, ...]:
    """Completed meetings, each as one (day, constant label) row."""

    meetings: list[PublicMeeting] = []
    for classified in selection.completed:
        if classified.event_type != EventType.MEETING:
            continue
        meetings.append(
            PublicMeeting(on=classified.occurrence.day(), label=classified.public_label or DEFAULT_MEETING_LABEL)
        )
    return tuple(meetings)


def public_meeting_days(selection: EventSelection) -> Mapping[str, int]:
    """Moscow days that carry a completed meeting, with its count."""

    days: dict[str, int] = {}
    for meeting in public_meetings(selection):
        key = meeting.on.isoformat()
        days[key] = days.get(key, 0) + 1
    return dict(sorted(days.items()))


# --------------------------------------------------------------------------- #
# Configuration from the process environment (stable IDs, no real values here)
# --------------------------------------------------------------------------- #

def _ids(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(part.strip() for part in value.replace(";", ",").split(",") if part.strip())


def _type_pairs(value: str | None) -> dict[str, EventType]:
    pairs: dict[str, EventType] = {}
    if not value:
        return pairs
    for chunk in value.replace(";", ",").split(","):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        subcalendar_id, _, raw = item.partition("=")
        subcalendar_id, raw = subcalendar_id.strip(), raw.strip()
        if not subcalendar_id or not raw:
            continue
        try:
            pairs[subcalendar_id] = EventType(raw)
        except ValueError as exc:
            raise EventError(f"unknown event type in {ENV_SUBCALENDAR_TYPES}: {raw!r}") from exc
    return pairs


def _match_mode(value: str | None) -> str:
    """The meeting-match mode; an unknown value is refused, never guessed."""

    mode = (value or DEFAULT_MEETING_MATCH).strip() or DEFAULT_MEETING_MATCH
    if mode not in MEETING_MATCH_MODES:
        raise EventError(f"unknown meeting match mode in {ENV_MEETING_MATCH}: {value!r}")
    return mode


def event_mapping_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    version: str = "env-config",
) -> EventTypeMapping:
    """Build the calendar mapping from the environment's stable-ID configuration.

    With nothing set, only the defaults apply: no meeting contour, no rent, no
    exclusion — nothing is promoted to the public calendar by accident.
    """

    env = os.environ if environ is None else environ
    titles = _ids(env.get(ENV_MEETING_TITLES))
    return EventTypeMapping(
        version=version,
        subcalendar_types=_type_pairs(env.get(ENV_SUBCALENDAR_TYPES)),
        private_subcalendars=_ids(env.get(ENV_PRIVATE_SUBCALENDARS)),
        excluded_subcalendars=_ids(env.get(ENV_EXCLUDED_SUBCALENDARS)),
        meeting_subcalendars=_ids(env.get(ENV_MEETING_SUBCALENDARS)),
        rent_subcalendars=_ids(env.get(ENV_RENT_SUBCALENDARS)),
        meeting_titles=titles or DEFAULT_MEETING_TITLES,
        meeting_label=(env.get(ENV_MEETING_LABEL) or DEFAULT_MEETING_LABEL).strip() or DEFAULT_MEETING_LABEL,
        meeting_match=_match_mode(env.get(ENV_MEETING_MATCH)),
    )


__all__ = [
    "ALL_TYPES",
    "CANCELLATION_MARKERS",
    "DEFAULT_MEETING_LABEL",
    "DEFAULT_MEETING_MATCH",
    "DEFAULT_MEETING_TITLE",
    "DEFAULT_MEETING_TITLES",
    "DERIVED_TYPES",
    "ENV_MEETING_MATCH",
    "EventError",
    "EventSelection",
    "EventStats",
    "EventType",
    "EventTypeMapping",
    "ClassifiedOccurrence",
    "MEETING_MATCH_MODES",
    "MeetingMatchError",
    "Occurrence",
    "PRIVATE_TYPES",
    "PUBLIC_TYPES",
    "PublicEvent",
    "PublicMeeting",
    "SOURCE_TYPES",
    "TRADITIONAL_TYPES",
    "TYPE_PRIORITY",
    "event_mapping_from_env",
    "event_stats",
    "public_calendar_days",
    "public_events",
    "public_meeting_days",
    "public_meetings",
    "select_completed",
]
