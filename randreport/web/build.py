"""Build public releases from the private store (the web side of the pipeline).

This mirrors what the nightly cycle does, but exposes it as a callable so a period
can be published on demand (for example while a month is being closed in the
editorial desk). It only reads the private store and calls the frozen domain; it
never touches a source. The scope policy and the calendar mapping are loaded from
the versioned mapping rows, so a release is always built with an explicit,
traceable mapping version.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..domain.events import EventType, EventTypeMapping
from ..domain.finance import ScopePolicy
from ..domain.flows import EconomicClass
from ..domain.timeframes import Period
from ..ops.pipeline import ReleaseBuilder
from ..ops.publish import publish_release
from ..store import repository as repo

KNOWN_CLASSES = {member.value for member in EconomicClass}
KNOWN_EVENT_TYPES = {member.value for member in EventType}


def scope_policy_from_payload(version: str, payload: Mapping[str, Any]) -> ScopePolicy:
    """Rebuild a :class:`ScopePolicy` from a stored mapping payload."""

    classes: dict[str, EconomicClass] = {}
    for category_id, value in (payload.get("category_classes") or {}).items():
        if value not in KNOWN_CLASSES:
            raise ValueError(f"mapping {version} has an unknown economic class {value!r}")
        classes[str(category_id)] = EconomicClass(value)
    return ScopePolicy(
        version=version,
        public_accounts=frozenset(payload.get("public_accounts") or ()),
        private_accounts=frozenset(payload.get("private_accounts") or ()),
        quarantined_accounts=frozenset(payload.get("quarantined_accounts") or ()),
        category_classes=classes,
        category_labels={str(k): str(v) for k, v in (payload.get("category_labels") or {}).items()},
        quarantined_categories=frozenset(payload.get("quarantined_categories") or ()),
        commission_categories=frozenset(payload.get("commission_categories") or ()),
        subsidy_category_ids=frozenset(payload.get("subsidy_category_ids") or ()),
        count_visibility_threshold_kopecks=int(payload.get("count_visibility_threshold_kopecks", 100_000)),
        sensitive_categories=frozenset(payload.get("sensitive_categories") or ()),
        unclassified_label=str(payload.get("unclassified_label", "требуют классификации")),
    )


def event_mapping_from_payload(version: str, payload: Mapping[str, Any]) -> EventTypeMapping:
    types: dict[str, EventType] = {}
    for subcalendar_id, value in (payload.get("subcalendar_types") or {}).items():
        if value not in KNOWN_EVENT_TYPES:
            raise ValueError(f"event mapping {version} has an unknown type {value!r}")
        types[str(subcalendar_id)] = EventType(value)
    return EventTypeMapping(
        version=version,
        subcalendar_types=types,
        private_subcalendars=frozenset(payload.get("private_subcalendars") or ()),
    )


def policy_from_store(conn: sqlite3.Connection) -> ScopePolicy:
    loaded = repo.load_category_mapping(conn)
    if loaded is None:
        # no mapping recorded yet: every category stays unclassified, money unknown
        return ScopePolicy(version="unmapped")
    version, payload = loaded
    return scope_policy_from_payload(version, payload)


def event_mapping_from_store(conn: sqlite3.Connection) -> EventTypeMapping:
    loaded = repo.load_event_mapping(conn)
    if loaded is None:
        return EventTypeMapping(version="unmapped")
    version, payload = loaded
    return event_mapping_from_payload(version, payload)


def build_and_publish(
    conn: sqlite3.Connection,
    *,
    release_dir: str | Path,
    periods: Sequence[Period],
    now: datetime | None = None,
    freshness: str = "fresh",
    verification: str = "operational",
    source_labels: Sequence[str] = (),
    finance_data_present: bool = True,
) -> list[tuple[str, str]]:
    """Build one release per period and publish each atomically. Returns (period, release_id)."""

    stamp = now or datetime.now(timezone.utc)
    policy = policy_from_store(conn)
    mapping = event_mapping_from_store(conn)
    published: list[tuple[str, str]] = []
    for period in periods:
        builder = ReleaseBuilder(policy=policy, event_mapping=mapping, period=period)
        release = builder.build(
            conn,
            cutoff=stamp,
            freshness=freshness,
            verification=verification,
            source_labels=tuple(source_labels),
            finance_data_present=finance_data_present,
        )
        repo.store_release(
            conn,
            release_id=release.release_id,
            as_of=release.as_of.isoformat(),
            schema_version="public-release-1",
            sections=tuple(release.payload_json),
            payload_text=release.combined_payload(),
            make_current=True,
        )
        publish_release(release_dir, release.release_id, release.files())
        published.append((period.label, release.release_id))
    return published
