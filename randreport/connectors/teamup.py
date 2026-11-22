"""Teamup read-only calendar client (docs/REQUIREMENTS.md section 7).

Only ``GET`` requests against ``api.teamup.com``. Two different strings are
never confused here:

* the *calendar key* is the secret of the calendar link. It only ever appears in
  the request path; it is not the local identity, it never reaches a log, a
  fixture, a stored row or a public payload;
* the *calendar id* is the stable internal identity of the calendar. It names
  the calendar for the store, the checks and the desk, and it does not move when
  the secret key is rotated - a rotation must not create a second calendar,
  orphan the local history or double-count it.

The ``version`` field of an event is an opaque revision token (observed in
staging as a hex string). It is kept verbatim, never parsed into a number and
never read as a time. Explicit deletions are read from the documented changelog
(``?modifiedSince=<unix timestamp>``), which cannot be combined with a date range
and covers at most 30 days; the date window itself is read with enough depth and
the store keeps the local archive, so retention on the source side never erases
our history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..domain.events import UNVERSIONED_REVISION, Occurrence
from ..domain.timeframes import MSK
from .transport import HttpStatusError, Request, Response, Transport, TransportError

DEFAULT_BASE_URL = "https://api.teamup.com"
#: The internal identity of the calendar when the deployment does not name one.
#: A constant on purpose: it must survive a rotation of the secret calendar key.
DEFAULT_CALENDAR_ID = "cal_teamup"
EVENTS_PATH = "/{calendar_key}/events"
TEAMUP_SOURCE = "teamup"
MAX_PAGES = 50
#: The documented depth of the Teamup changelog: ``modifiedSince`` may be at most
#: 30 days in the past, and the endpoint refuses a date range entirely.
CHANGELOG_MAX_AGE_S = 30 * 24 * 60 * 60


class TeamupProtocolError(RuntimeError):
    """A response that does not match the documented event schema."""


class TeamupPaginationError(RuntimeError):
    """The pagination loop did not terminate inside the documented limit."""


def parse_revision(raw: Any) -> str:
    """Normalize the API ``version`` field into an opaque revision token.

    The token is kept verbatim: it is never converted to a number (``int(...)``
    or a base-16 parse), never collapsed to ``1`` and never read as a timestamp.
    A scalar is required; a record whose source omits the field gets the
    documented :data:`~randreport.domain.events.UNVERSIONED_REVISION` token.
    """

    if raw is None:
        return UNVERSIONED_REVISION
    if isinstance(raw, bool):
        raise TeamupProtocolError(f"unexpected revision {raw!r}")
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str):
        return raw.strip() or UNVERSIONED_REVISION
    raise TeamupProtocolError(f"unexpected revision {raw!r}")


@dataclass(frozen=True)
class TeamupCounts:
    events: int
    deleted: int
    pages: int


@dataclass(frozen=True)
class TeamupWindow:
    occurrences: tuple[Occurrence, ...]
    deleted_ids: tuple[str, ...]
    counts: TeamupCounts
    raw: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


def _parse_dt(value: Any, *, all_day: bool) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TeamupProtocolError(f"unexpected datetime {value!r}")
    if all_day and "T" not in value:
        return datetime.combine(date.fromisoformat(value), time(0, 0), tzinfo=MSK)
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MSK)
    return moment


class TeamupClient:
    """Read-only Teamup events client with pagination."""

    source = TEAMUP_SOURCE

    def __init__(
        self,
        transport: Transport,
        *,
        calendar_key: str,
        calendar_id: str | None = None,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 20.0,
        max_pages: int = MAX_PAGES,
    ) -> None:
        if not calendar_key or "/" in calendar_key:
            raise ValueError("a calendar key is required and must not contain '/'")
        identity = DEFAULT_CALENDAR_ID if calendar_id is None else str(calendar_id).strip()
        if not identity or "/" in identity:
            raise ValueError("an internal calendar id is required and must not contain '/'")
        if calendar_key in identity:
            raise ValueError("the secret calendar key must not be part of the internal calendar id")
        self._transport = transport
        self._calendar_key = calendar_key
        self._calendar_id = identity
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_pages = max_pages

    def __repr__(self) -> str:
        return (
            f"TeamupClient(base_url={self._base_url!r}, calendar_id={self._calendar_id!r},"
            f" calendar_key=set, token={'set' if self._token else None})"
        )

    @property
    def calendar_id(self) -> str:
        """The stable internal calendar identity - never the secret key.

        It is what the store, the checks and the desk use; it does not change when
        the secret calendar key is rotated, so a rotation neither creates a second
        calendar nor orphans the local history.
        """

        return self._calendar_id

    def fetch_window(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> TeamupWindow:
        """Fetch all pages of a date window (instances, including recurring ones).

        No change filter is sent: the documented changed-events call
        (``?modifiedSince=<unix timestamp>``) **cannot be combined with a date
        range** — the API answers 400 "Invalid request" — and it returns the
        master record of a repeating series instead of its instances. The
        changelog is therefore read separately (:meth:`fetch_deleted_since`), and
        the local archive resolves what a window no longer contains.
        """

        if end_date < start_date:
            raise ValueError("end date is before start date")
        occurrences: list[Occurrence] = []
        deleted_ids: list[str] = []
        raw_events: list[Mapping[str, Any]] = []
        page = 1
        pages = 0

        while page is not None:
            pages += 1
            if pages > self._max_pages:
                raise TeamupPaginationError("events pagination did not terminate within the limit")
            params = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "page": str(page),
            }
            response = self._get(EVENTS_PATH.format(calendar_key=self._calendar_key), params)
            if not response.ok:
                raise HttpStatusError(response.status, response.text())
            try:
                payload = response.json()
            except ValueError as exc:  # pragma: no cover - defensive
                raise TeamupProtocolError("events response is not JSON") from exc
            events = payload.get("events")
            if events is None or not isinstance(events, (list, tuple)):
                raise TeamupProtocolError("events response without an events list")
            for raw in events:
                raw_events.append(raw)
                if raw.get("delete_dt"):
                    deleted_ids.append(self._deleted_id(raw))
                    continue
                occurrences.append(self._normalize_event(raw))
            page = self._next_page(payload)

        return TeamupWindow(
            occurrences=tuple(occurrences),
            deleted_ids=tuple(deleted_ids),
            counts=TeamupCounts(events=len(occurrences), deleted=len(deleted_ids), pages=pages),
            raw=tuple(raw_events),
        )

    def fetch_deleted_since(self, *, modified_since: int, now: int | None = None) -> tuple[str, ...]:
        """Ids explicitly deleted on the source since a server timestamp.

        The documented changelog call, used for one thing only: an explicit
        deletion. A record deleted on the source simply disappears from a date
        window (where it is indistinguishable from source retention), so the
        tombstone in the changelog is what tells them apart.

        The changelog covers at most 30 days, so an older cursor must not be sent
        (the API answers 400). ``now`` is the moment the cycle runs (its cutoff),
        which is what the age is measured against; it defaults to the wall clock.
        """

        if not isinstance(modified_since, int) or modified_since < 0:
            raise ValueError("modifiedSince must be a unix timestamp")
        if self.changelog_age_seconds(modified_since, now=now) > CHANGELOG_MAX_AGE_S:
            raise ValueError("the changelog does not cover a timestamp older than 30 days")
        params = {"modifiedSince": str(modified_since)}
        response = self._get(EVENTS_PATH.format(calendar_key=self._calendar_key), params)
        if not response.ok:
            raise HttpStatusError(response.status, response.text())
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise TeamupProtocolError("changes response is not JSON") from exc
        events = payload.get("events")
        if events is None or not isinstance(events, (list, tuple)):
            raise TeamupProtocolError("changes response without an events list")
        return tuple(self._deleted_id(raw) for raw in events if raw.get("delete_dt"))

    @staticmethod
    def changelog_age_seconds(modified_since: int, *, now: int | None = None) -> int:
        """How far in the past a cursor is, so the caller can honour the 30 days."""

        current = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
        return current - int(modified_since)

    def _get(self, path: str, params: Mapping[str, str]) -> Response:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Teamup-Token"] = self._token
        request = Request(
            method="GET",
            url=f"{self._base_url}{path}",
            params=params,
            headers=headers,
            timeout_s=self._timeout_s,
        )
        try:
            return self._transport.send(request)
        except HttpStatusError:
            raise
        except TransportError as exc:  # pragma: no cover - network path
            raise TransportError(str(exc)) from exc

    @staticmethod
    def _next_page(payload: Mapping[str, Any]) -> int | None:
        pages = payload.get("pages")
        if not isinstance(pages, Mapping):
            return None
        next_page = pages.get("next")
        if next_page in (None, ""):
            return None
        if isinstance(next_page, Mapping):
            candidate = next_page.get("page")
            return int(candidate) if candidate is not None else None
        return int(next_page)

    @staticmethod
    def _deleted_id(raw: Mapping[str, Any]) -> str:
        """The id of a tombstone; a deletion without an id would invent ``"None"``."""

        if raw.get("id") in (None, ""):
            raise TeamupProtocolError("deletion without an id")
        return str(raw["id"])

    def _normalize_event(self, raw: Mapping[str, Any]) -> Occurrence:
        if raw.get("id") in (None, ""):
            raise TeamupProtocolError("event without an id")
        all_day = bool(raw.get("all_day"))
        start = _parse_dt(raw.get("start_dt"), all_day=all_day)
        if start is None:
            raise TeamupProtocolError("event without a start")
        end = _parse_dt(raw.get("end_dt"), all_day=all_day)
        subcalendars: Sequence[Any] = raw.get("subcalendar_ids") or ()
        if not isinstance(subcalendars, (list, tuple)):
            raise TeamupProtocolError("subcalendar_ids must be a list")
        return Occurrence(
            calendar_id=self._calendar_id,
            occurrence_id=str(raw["id"]),
            start=start,
            end=end,
            subcalendar_ids=tuple(str(sid) for sid in subcalendars),
            series_id=None if raw.get("series_id") in (None, "") else str(raw["series_id"]),
            title=None if raw.get("title") is None else str(raw["title"]),
            all_day=all_day,
            raw_type=None if raw.get("custom_type") is None else str(raw.get("custom_type")),
            revision=parse_revision(raw.get("version")),
            deleted=False,
        )

    @staticmethod
    def default_window(cutoff: datetime, *, depth_days: int = 400) -> tuple[date, date]:
        """A window wide enough to catch moves without drowning in history."""

        end = cutoff.astimezone(MSK).date()
        return end - timedelta(days=depth_days), end
