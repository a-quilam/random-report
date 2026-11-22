"""Read the published allowlist projection a page is allowed to render.

The public site never queries a source and never computes money or events. It
reads the release files written atomically by the nightly builder and re-hydrates
them through the *same* DTO classes they were serialised from. That round-trip is
not decoration: ``from_public_dict`` rejects any field outside the frozen
allowlist, so a release that somehow grew an extra field cannot be rendered at
all (docs/DOMAIN.md sections 8-9).

Layout on disk (``ops.publish``)::

    <root>/
        current -> <release_id>/
        <release_id>/index.json money.json events.json community.json chronicle.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from ..domain.dto import (
    ChronicleDTO,
    CommunityDTO,
    EventsDTO,
    MoneyDTO,
    StrictDTO,
)

SECTION_TYPES: Mapping[str, type] = {
    "money": MoneyDTO,
    "events": EventsDTO,
    "community": CommunityDTO,
    "chronicle": ChronicleDTO,
}

CURRENT = "current"

#: The file in a release that carries the allowlisted version archive of period
#: reports. The public process renders ``/reports/*`` from it, so it never has to
#: open the private database.
REPORTS_FILE = "reports.json"


class ReleaseReadError(RuntimeError):
    """A release on disk is missing, unreadable or outside the allowlist."""


@dataclass(frozen=True)
class Release:
    release_id: str
    as_of: str
    sections: Mapping[str, StrictDTO]

    @property
    def money(self) -> MoneyDTO | None:
        dto = self.sections.get("money")
        return dto if isinstance(dto, MoneyDTO) else None

    @property
    def events(self) -> EventsDTO | None:
        dto = self.sections.get("events")
        return dto if isinstance(dto, EventsDTO) else None

    @property
    def community(self) -> CommunityDTO | None:
        dto = self.sections.get("community")
        return dto if isinstance(dto, CommunityDTO) else None

    @property
    def chronicle(self) -> ChronicleDTO | None:
        dto = self.sections.get("chronicle")
        return dto if isinstance(dto, ChronicleDTO) else None

    @property
    def period_label(self) -> str | None:
        money = self.money
        return money.period.requested if money is not None else None

    def freshness(self) -> str:
        money = self.money
        return money.freshness if money is not None else "unknown"

    def verification(self) -> str:
        money = self.money
        return money.verification if money is not None else "unknown"


class ReleaseIndex:
    """A read-only view over the directory the nightly builder publishes into."""

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None

    # --- discovery -------------------------------------------------------- #

    def exists(self) -> bool:
        return bool(self.root and self.root.exists())

    def ids(self) -> tuple[str, ...]:
        if not self.exists():
            return ()
        assert self.root is not None
        found = [
            entry.name
            for entry in sorted(self.root.iterdir())
            if entry.is_dir() and not entry.name.startswith(".") and entry.name.startswith("rel_")
        ]
        return tuple(found)

    def current_id(self) -> str | None:
        if not self.exists():
            return None
        assert self.root is not None
        link = self.root / CURRENT
        try:
            target = os.readlink(link)
        except OSError:
            manifest = link / "index.json"
            if manifest.exists():
                return json.loads(manifest.read_text(encoding="utf-8")).get("release_id")
            return None
        return Path(target).name

    # --- loading ---------------------------------------------------------- #

    def _load_release(self, release_id: str) -> Release | None:
        if not self.exists():
            return None
        assert self.root is not None
        base = self.root / release_id
        index_file = base / "index.json"
        if not index_file.exists():
            return None
        header = json.loads(index_file.read_text(encoding="utf-8"))
        sections: dict[str, StrictDTO] = {}
        for name, dto_type in SECTION_TYPES.items():
            path = base / f"{name}.json"
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                sections[name] = dto_type.from_public_dict(payload)
            except Exception as exc:  # noqa: BLE001 - a broken release must not crash the site
                raise ReleaseReadError(f"release {release_id}: section {name} failed the allowlist: {exc}") from exc
        return Release(release_id=release_id, as_of=str(header.get("as_of", "")), sections=sections)

    def current(self) -> Release | None:
        release_id = self.current_id()
        return self._load_release(release_id) if release_id else None

    def get(self, release_id: str) -> Release | None:
        return self._load_release(release_id)

    def by_period(self, period_label: str) -> Release | None:
        """The newest published release whose money section covers ``period_label``."""

        for release_id in reversed(self.ids()):
            release = self._load_release(release_id)
            if release is None:
                continue
            money = release.money
            if money is not None and money.period.requested == period_label:
                return release
        return None

    def periods(self) -> tuple[str, ...]:
        out: list[str] = []
        for release_id in self.ids():
            release = self._load_release(release_id)
            if release is None or release.period_label is None:
                continue
            if release.period_label not in out:
                out.append(release.period_label)
        return tuple(out)

    # --- the public report archive (no database) --------------------------- #

    def reports(self) -> Mapping[str, Any]:
        """The allowlisted version archive of period reports, or ``{}``.

        Read from the current release; when that release predates the archive, the
        newest release that carries one is used, so a rollback to an older release
        never takes ``/reports/*`` down with it.
        """

        if not self.exists():
            return {}
        assert self.root is not None
        current = self.current_id()
        order = [current, *reversed(self.ids())] if current else list(reversed(self.ids()))
        seen: set[str] = set()
        for release_id in order:
            if release_id is None or release_id in seen:
                continue
            seen.add(release_id)
            path = self.root / release_id / REPORTS_FILE
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # an empty archive means "this release predates the archive"; keep
            # looking so a rollback to an older release never drops /reports/*
            if isinstance(data, Mapping) and data:
                return data
        return {}

    def report_entry(self, period_label: str) -> Mapping[str, Any] | None:
        entry = self.reports().get(period_label)
        return entry if isinstance(entry, Mapping) else None


def release_mtime(root: str | Path | None) -> datetime | None:
    """When the served release was published, for a cache buster / build stamp."""

    if not root:
        return None
    link = Path(root) / CURRENT
    try:
        stat = link.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(stat.st_mtime).astimezone()


def period_of(release: Release) -> str | None:
    money = release.money
    return money.period.requested if money is not None else None


def as_of_date(release: Release) -> date | None:
    money = release.money
    return money.as_of if money is not None else None


def overview(release: Release) -> dict[str, Any]:
    """The small combined view the homepage and ``/api/public/overview`` share."""

    money = release.money
    events = release.events
    community = release.community
    return {
        "release_id": release.release_id,
        "period": money.period.requested if money else None,
        "as_of": money.as_of.isoformat() if money else None,
        "freshness": release.freshness(),
        "verification": release.verification(),
        "metrics": dict(money.metrics) if money else {},
        "categories": [c.to_public_dict() for c in money.categories] if money else [],
        "events_metrics": dict(events.metrics) if events else {},
        "event_type_aggregates": dict(events.event_type_aggregates) if events else {},
        "community_metrics": dict(community.metrics) if community else {},
        "source_labels": list(money.source_labels) if money else [],
    }
