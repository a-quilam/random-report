"""Teamup read-only calendar client (docs/REQUIREMENTS.md section 7).

Only ``GET`` requests against ``api.teamup.com``. The calendar key is a secret
that never appears in a log, a fixture or a public payload. Incremental reads use
``modifiedSince``; when the changelog is unreliable the caller instead asks for
overlapping date windows and the store keeps the local archive, so retention on
the source side never erases our history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping, Sequence

from ..domain.events import Occurrence
from ..domain.timeframes import MSK
from .transport import HttpStatusError, Request, Response, Transport, TransportError

DEFAULT_BASE_URL = "https://api.teamup.com"
EVENTS_PATH = "/{calendar_key}/events"
TEAMUP_SOURCE = "teamup"
MAX_PAGES = 50


class TeamupProtocolError(RuntimeError):
    """A response that does not match the documented event schema."""


class TeamupPaginationError(RuntimeError):
    """The pagination loop did not terminate inside the documented limit."""


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
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 20.0,
        max_pages: int = MAX_PAGES,
    ) -> None:
        if not calendar_key or "/" in calendar_key:
            raise ValueError("a calendar key is required and must not contain '/'")
        self._transport = transport
        self._calendar_key = calendar_key
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_pages = max_pages

    def __repr__(self) -> str:
        return f"TeamupClient(base_url={self._base_url!r}, calendar_key=set, token={'set' if self._token else None})"

    @property
    def calendar_id(self) -> str:
        """The stable calendar key used as the local calendar id (never published)."""

        return self._calendar_key

    def fetch_window(
        self,
        *,
        start_date: date,
        end_date: date,
        modified_since: datetime | None = None,
    ) -> TeamupWindow:
        """Fetch all pages of a date window (optionally only modified records)."""

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
            if modified_since is not None:
                params["modifiedSince"] = str(int(modified_since.timestamp()))
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
                    deleted_ids.append(str(raw.get("id")))
                    continue
                occurrences.append(self._normalize_event(raw))
            page = self._next_page(payload)

        return TeamupWindow(
            occurrences=tuple(occurrences),
            deleted_ids=tuple(deleted_ids),
            counts=TeamupCounts(events=len(occurrences), deleted=len(deleted_ids), pages=pages),
            raw=tuple(raw_events),
        )

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

    def _normalize_event(self, raw: Mapping[str, Any]) -> Occurrence:
        if "id" not in raw:
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
            calendar_id=self._calendar_key,
            occurrence_id=str(raw["id"]),
            start=start,
            end=end,
            subcalendar_ids=tuple(str(sid) for sid in subcalendars),
            series_id=None if raw.get("series_id") in (None, "") else str(raw["series_id"]),
            title=None if raw.get("title") is None else str(raw["title"]),
            all_day=all_day,
            raw_type=None if raw.get("custom_type") is None else str(raw.get("custom_type")),
            revision=int(raw.get("version") or 1),
            deleted=False,
        )

    @staticmethod
    def default_window(cutoff: datetime, *, depth_days: int = 400) -> tuple[date, date]:
        """A window wide enough to catch moves without drowning in history."""

        end = cutoff.astimezone(MSK).date()
        return end - timedelta(days=depth_days), end
