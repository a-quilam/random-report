"""Calendar rules: which occurrences actually happened and what may be published.

Completed means: the record exists in the calendar (or in the local archive after
source retention) at the moment of a successful sync and its end is at or before
the run cutoff. No mandatory organiser confirmation, no attendance, no LLM
selection. Closed and internal records are counted in aggregates and never
published with a title, date, id, duration or calendar grid point.

See docs/REQUIREMENTS.md sections 3.3, 7, 13 (E01-E03) and docs/DOMAIN.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Mapping, Sequence

from .money import share_percent
from .timeframes import MSK, msk_day


class EventType(StrEnum):
    OPEN = "open"
    OPEN_BY_REGISTRATION = "open_by_registration"
    CLOSED = "closed"
    INTERNAL_MEETING = "internal_meeting"
    PREPARATION = "preparation"
    UNKNOWN = "unknown"


TRADITIONAL_TYPES = (
    EventType.OPEN,
    EventType.OPEN_BY_REGISTRATION,
    EventType.CLOSED,
    EventType.INTERNAL_MEETING,
    EventType.PREPARATION,
)
ALL_TYPES = TRADITIONAL_TYPES + (EventType.UNKNOWN,)
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
    revision: int = 1
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
    version: str = "mapping"
    subcalendar_types: Mapping[str, EventType] = field(default_factory=dict)
    private_subcalendars: frozenset[str] = frozenset()

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


@dataclass(frozen=True)
class ClassifiedOccurrence:
    occurrence: Occurrence
    event_type: EventType
    public: bool
    reason: str


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

    ordered = sorted(occurrences, key=lambda occ: (occ.key, occ.revision))
    revision_log = tuple(ordered)

    latest: dict[tuple[str, str], Occurrence] = {}
    superseded: list[Occurrence] = []
    for occurrence in ordered:
        current = latest.get(occurrence.key)
        if current is None or occurrence.revision > current.revision:
            if current is not None:
                superseded.append(current)
            latest[occurrence.key] = occurrence
        else:
            superseded.append(occurrence)

    completed: list[ClassifiedOccurrence] = []
    ongoing: list[Occurrence] = []
    future: list[Occurrence] = []
    deleted: list[Occurrence] = []
    unknown_end: list[Occurrence] = []
    local_archive_only: list[Occurrence] = []
    admin_flags: list[str] = []

    for occurrence in latest.values():
        if occurrence.deleted:
            deleted.append(occurrence)
            continue
        event_type = mapping.event_type(occurrence.subcalendar_ids)
        # published with date and title only for genuinely public formats; closed,
        # internal, preparation and unknown formats stay aggregate-only
        public = event_type in PUBLIC_TYPES and not mapping.is_private(occurrence.subcalendar_ids)
        if _mentions_cancellation(occurrence.title):
            # the source mapping wins; a possible problem is surfaced to admins, never guessed away
            admin_flags.append(occurrence.occurrence_id)
        classified = ClassifiedOccurrence(
            occurrence=occurrence,
            event_type=event_type,
            public=public,
            reason="subcalendar_mapping" if occurrence.subcalendar_ids else "no_subcalendar",
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

    def public_payload(self) -> dict:
        """Aggregate-only view: type counts and totals, never a closed detail."""

        return {
            "total_events": self.total,
            "event_type_aggregates": dict(self.by_type),
            "public_events": self.public_total,
            "closed_aggregate": self.closed_total,
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
    return EventStats(
        cutoff=cutoff or selection.cutoff,
        total=total,
        by_type=by_type,
        public_total=public_total,
        public_share_percent=share_percent(public_total, total) if total else None,
        closed_total=by_type[EventType.CLOSED.value],
        local_archive_total=len(selection.local_archive_only),
    )


def public_calendar_days(selection: EventSelection) -> Mapping[str, int]:
    """Moscow days with completed *public* events only.

    Closed/internal days are absent on purpose: a zero would let anyone recover a
    closed date by subtracting the totals from a public calendar.
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
