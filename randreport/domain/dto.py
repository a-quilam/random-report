"""Strict public DTOs (the closed allowlist of docs/REQUIREMENTS.md section 10).

Every payload that leaves the domain layer is built from these classes. Unknown
fields are rejected on the way in and out, floats are rejected everywhere, money
crosses the boundary as integer kopecks plus an explicit display string, and raw
source entities (accounts, transactions, closed occurrences, calendar ids) have
no field to travel in.

The web/SSR layer must serialise these objects and nothing else; see docs/DOMAIN.md.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Mapping, Sequence


class DTOError(ValueError):
    """Base class for public-contract violations."""


class UnknownFieldError(DTOError):
    """A field outside the allowlist was present."""


class InvalidFieldError(DTOError):
    """A field had a type or value the public contract does not accept."""


class StrictDTO:
    """Frozen dataclass with an allowlisted, type-checked public representation."""

    NESTED: ClassVar[Mapping[str, type]] = {}
    ELEMENTS: ClassVar[Mapping[str, type]] = {}
    SCALARS: ClassVar[Mapping[str, str]] = {}

    def to_public_dict(self) -> dict[str, Any]:
        return {
            f.metadata.get("public_name", f.name): encode_public_value(getattr(self, f.name))
            for f in dataclasses.fields(self)
        }

    @classmethod
    def _allowed(cls) -> dict[str, str]:
        return {f.metadata.get("public_name", f.name): f.name for f in dataclasses.fields(cls)}

    @classmethod
    def from_public_dict(cls, data: Mapping[str, Any]) -> "StrictDTO":
        if not isinstance(data, Mapping):
            raise InvalidFieldError(f"{cls.__name__} expects a mapping")
        allowed = cls._allowed()
        unknown = sorted(set(data) - set(allowed))
        if unknown:
            raise UnknownFieldError(f"{cls.__name__}: fields outside the allowlist: {unknown}")
        kwargs: dict[str, Any] = {}
        for public_name, value in data.items():
            kwargs[allowed[public_name]] = cls._decode_field(allowed[public_name], value)
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise InvalidFieldError(f"{cls.__name__}: {exc}") from exc

    @classmethod
    def _decode_field(cls, name: str, value: Any) -> Any:
        if name in cls.NESTED:
            if value is None:
                return None
            return cls.NESTED[name].from_public_dict(value)
        if name in cls.ELEMENTS:
            if value is None:
                return ()
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise InvalidFieldError(f"{cls.__name__}.{name} must be a list")
            return tuple(cls.ELEMENTS[name].from_public_dict(item) for item in value)
        return decode_scalar(value, cls.SCALARS.get(name, "any"), owner=f"{cls.__name__}.{name}")


def encode_public_value(value: Any) -> Any:
    """Encode a value for the public contract; floats are never allowed."""

    if isinstance(value, StrictDTO):
        return value.to_public_dict()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): encode_public_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode_public_value(item) for item in value]
    if isinstance(value, float):
        raise InvalidFieldError("floats are not allowed in public payloads")
    raise InvalidFieldError(f"unsupported public value of type {type(value).__name__}")


def decode_scalar(value: Any, kind: str, *, owner: str = "value") -> Any:
    if isinstance(value, float):
        raise InvalidFieldError(f"{owner}: floats are not allowed in public payloads")
    if kind == "date":
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise InvalidFieldError(f"{owner}: not an ISO date") from exc
        raise InvalidFieldError(f"{owner}: expected an ISO date string")
    if kind == "datetime":
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise InvalidFieldError(f"{owner}: not an ISO datetime") from exc
        raise InvalidFieldError(f"{owner}: expected an ISO datetime string")
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidFieldError(f"{owner}: expected an integer")
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise InvalidFieldError(f"{owner}: expected a string")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise InvalidFieldError(f"{owner}: expected a boolean")
        return value
    if kind == "str_tuple":
        if value is None:
            return ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise InvalidFieldError(f"{owner}: expected a list of strings")
        if not all(isinstance(item, str) for item in value):
            raise InvalidFieldError(f"{owner}: expected a list of strings")
        return tuple(value)
    if kind == "mapping":
        if not isinstance(value, Mapping):
            raise InvalidFieldError(f"{owner}: expected a mapping")
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidFieldError(f"{owner}: mapping keys must be strings")
            if isinstance(item, float):
                raise InvalidFieldError(f"{owner}: floats are not allowed in public payloads")
        return dict(value)
    return value


@dataclass(frozen=True)
class PeriodDTO(StrictDTO):
    requested: str
    covered_start: date | None
    covered_end: date | None
    complete: bool

    SCALARS: ClassVar[Mapping[str, str]] = {"covered_start": "date", "covered_end": "date"}


@dataclass(frozen=True)
class CategoryDTO(StrictDTO):
    key: str
    label: str
    bucket: str
    direction: str
    amount_kopecks: int
    amount_rub: str
    share_percent: str | None
    operations_count: int | None
    #: An approved optional hint for this category; the UI must reach it by touch
    #: and keyboard (docs/ALLOCATION.md section 5).  ``None`` means the owner
    #: approved no hint — the UI must not invent one.
    hint: str | None = None

    SCALARS: ClassVar[Mapping[str, str]] = {"amount_kopecks": "int"}


@dataclass(frozen=True)
class AllocationAdjustmentDTO(StrictDTO):
    """One allocation override, described in public-safe terms."""

    kind: str
    category_key: str
    period: str | None
    before_kopecks: int
    after_kopecks: int
    note: str

    SCALARS: ClassVar[Mapping[str, str]] = {"before_kopecks": "int", "after_kopecks": "int"}


@dataclass(frozen=True)
class RentMonthDTO(StrictDTO):
    """The agreed rent and the actual bank payment of one month, side by side."""

    month: str
    computed_kopecks: int
    bank_kopecks: int
    applied: bool
    note: str

    SCALARS: ClassVar[Mapping[str, str]] = {
        "computed_kopecks": "int",
        "bank_kopecks": "int",
        "applied": "bool",
    }


@dataclass(frozen=True)
class AllocationDTO(StrictDTO):
    """The owner's allocation overrides as the public contract sees them.

    Every field is a count, a kopeck total or a label — there is no field for a
    raw category id, a source account or an operation, so the private detail has
    nowhere to travel.
    """

    version: str = "allocation"
    confirmed: bool = True
    applied: bool = False
    excluded_categories: int = 0
    excluded_operations: int = 0
    shared_expenses_before_kopecks: int = 0
    shared_expenses_after_kopecks: int = 0
    computed_rent_kopecks: int = 0
    bank_rent_kopecks: int = 0
    rent_applied: bool = False
    rent_differs: bool = False
    corrections_positive_kopecks: int = 0
    corrections_negative_kopecks: int = 0
    corrections_unclassified: bool = False
    adjustments: tuple["AllocationAdjustmentDTO", ...] = ()
    rent_months: tuple["RentMonthDTO", ...] = ()
    labels: tuple[str, ...] = ()

    NESTED: ClassVar[Mapping[str, type]] = {}
    ELEMENTS: ClassVar[Mapping[str, type]] = {
        "adjustments": AllocationAdjustmentDTO,
        "rent_months": RentMonthDTO,
    }
    SCALARS: ClassVar[Mapping[str, str]] = {
        "confirmed": "bool",
        "applied": "bool",
        "excluded_categories": "int",
        "excluded_operations": "int",
        "shared_expenses_before_kopecks": "int",
        "shared_expenses_after_kopecks": "int",
        "computed_rent_kopecks": "int",
        "bank_rent_kopecks": "int",
        "rent_applied": "bool",
        "rent_differs": "bool",
        "corrections_positive_kopecks": "int",
        "corrections_negative_kopecks": "int",
        "corrections_unclassified": "bool",
        "labels": "str_tuple",
    }


@dataclass(frozen=True)
class MoneyDTO(StrictDTO):
    period: PeriodDTO
    as_of: date
    release_id: str
    freshness: str
    verification: str
    completeness: str
    source_labels: tuple[str, ...]
    unit: str
    metrics: Mapping[str, Any]
    categories: tuple[CategoryDTO, ...]
    allocation: AllocationDTO = field(default_factory=AllocationDTO)

    NESTED: ClassVar[Mapping[str, type]] = {"period": PeriodDTO, "allocation": AllocationDTO}
    ELEMENTS: ClassVar[Mapping[str, type]] = {"categories": CategoryDTO}
    SCALARS: ClassVar[Mapping[str, str]] = {
        "as_of": "date",
        "metrics": "mapping",
        "source_labels": "str_tuple",
    }


@dataclass(frozen=True)
class PublicEventDTO(StrictDTO):
    on: date
    type: str
    title: str
    description: str | None
    announce_url: str | None
    registration_open: bool

    SCALARS: ClassVar[Mapping[str, str]] = {"on": "date", "registration_open": "bool"}


@dataclass(frozen=True)
class EventTypeAggregateDTO(StrictDTO):
    type: str
    count: int
    public: bool

    SCALARS: ClassVar[Mapping[str, str]] = {"count": "int", "public": "bool"}


@dataclass(frozen=True)
class PublicMeetingDTO(StrictDTO):
    """A meeting of the allowed closed contour: its Moscow start day and a constant label.

    There is deliberately no field for a raw title, id, time, url, description or
    people — the source detail has nowhere to travel.
    """

    on: date
    label: str

    SCALARS: ClassVar[Mapping[str, str]] = {"on": "date"}


@dataclass(frozen=True)
class EventsDTO(StrictDTO):
    period: PeriodDTO
    as_of: date
    release_id: str
    freshness: str
    verification: str
    completeness: str
    source_labels: tuple[str, ...]
    public_events: tuple[PublicEventDTO, ...]
    meetings: tuple[PublicMeetingDTO, ...]
    event_type_aggregates: Mapping[str, int]
    event_type_details: tuple[EventTypeAggregateDTO, ...]
    calendar_days: Mapping[str, int]
    meeting_days: Mapping[str, int]
    calendar_granularity: str
    metrics: Mapping[str, Any]

    NESTED: ClassVar[Mapping[str, type]] = {"period": PeriodDTO}
    ELEMENTS: ClassVar[Mapping[str, type]] = {
        "public_events": PublicEventDTO,
        "meetings": PublicMeetingDTO,
        "event_type_details": EventTypeAggregateDTO,
    }
    SCALARS: ClassVar[Mapping[str, str]] = {
        "as_of": "date",
        "event_type_aggregates": "mapping",
        "calendar_days": "mapping",
        "meeting_days": "mapping",
        "metrics": "mapping",
        "source_labels": "str_tuple",
    }


@dataclass(frozen=True)
class ChannelPointDTO(StrictDTO):
    on: date
    subscribers: int
    source: str

    SCALARS: ClassVar[Mapping[str, str]] = {"on": "date", "subscribers": "int"}


@dataclass(frozen=True)
class CommunityDTO(StrictDTO):
    period: PeriodDTO
    as_of: date
    release_id: str
    freshness: str
    verification: str
    completeness: str
    source_labels: tuple[str, ...]
    channel_points: tuple[ChannelPointDTO, ...]
    metrics: Mapping[str, Any]

    NESTED: ClassVar[Mapping[str, type]] = {"period": PeriodDTO}
    ELEMENTS: ClassVar[Mapping[str, type]] = {"channel_points": ChannelPointDTO}
    SCALARS: ClassVar[Mapping[str, str]] = {"as_of": "date", "metrics": "mapping", "source_labels": "str_tuple"}


@dataclass(frozen=True)
class NewsItemDTO(StrictDTO):
    id: str
    title: str
    summary: str
    category: str
    published_on: date
    source_urls: tuple[str, ...]
    machine_processed: bool
    verified: bool

    SCALARS: ClassVar[Mapping[str, str]] = {
        "published_on": "date",
        "source_urls": "str_tuple",
        "machine_processed": "bool",
        "verified": "bool",
    }


@dataclass(frozen=True)
class ChronicleDTO(StrictDTO):
    period: PeriodDTO
    as_of: date
    release_id: str
    freshness: str
    verification: str
    completeness: str
    source_labels: tuple[str, ...]
    news_items: tuple[NewsItemDTO, ...]
    metrics: Mapping[str, Any]

    NESTED: ClassVar[Mapping[str, type]] = {"period": PeriodDTO}
    ELEMENTS: ClassVar[Mapping[str, type]] = {"news_items": NewsItemDTO}
    SCALARS: ClassVar[Mapping[str, str]] = {"as_of": "date", "metrics": "mapping", "source_labels": "str_tuple"}
