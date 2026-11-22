"""The confirmed public-contour mapping, loaded once and shared.

One loader is used by every producer of a public release — the nightly CLI, the
on-demand web builder and the editorial desk — so a release is always built from
the *same* confirmed map, and never from a hardcoded default.

Precedence (docs/DOMAIN.md section 4):

1. the newest mapping recorded in the private store by the editorial desk is
   authoritative — loading never writes, so a desk edit is never overwritten;
2. otherwise a private configuration file (``RANREPORT_FINANCE_POLICY`` /
   ``--policy``) may supply the same payload;
3. otherwise the contour is *unconfirmed*: ``contour_confirmed=False``, which
   makes every money indicator unknown instead of zero and never counts an
   unmapped category as a donation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from ..domain.events import (
    DEFAULT_MEETING_LABEL,
    DEFAULT_MEETING_MATCH,
    DEFAULT_MEETING_TITLES,
    MEETING_MATCH_MODES,
    SOURCE_TYPES,
    EventType,
    EventTypeMapping,
    event_mapping_from_env,
)
from ..domain.finance import ScopePolicy
from ..domain.flows import EconomicClass
from ..store import repository as repo

KNOWN_CLASSES = frozenset(member.value for member in EconomicClass)
# Only a *source* subcalendar may be mapped by hand; ``meeting``/``closed_rent`` are
# derived public categories of the closed contour and are never assigned directly.
KNOWN_EVENT_TYPES = frozenset(event_type.value for event_type in SOURCE_TYPES)

UNMAPPED_VERSION = "unmapped"


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
        contour_confirmed=bool(payload.get("contour_confirmed", True)),
    )


def _ids(payload: Mapping[str, Any], key: str) -> frozenset[str]:
    return frozenset(str(item) for item in (payload.get(key) or ()) if str(item))


def event_mapping_from_payload(version: str, payload: Mapping[str, Any]) -> EventTypeMapping:
    types: dict[str, EventType] = {}
    for subcalendar_id, value in (payload.get("subcalendar_types") or {}).items():
        if value not in KNOWN_EVENT_TYPES:
            raise ValueError(f"event mapping {version} has an unknown type {value!r}")
        types[str(subcalendar_id)] = EventType(value)
    titles = _ids(payload, "meeting_titles")
    raw_match = str(payload.get("meeting_match") or "").strip() or DEFAULT_MEETING_MATCH
    if raw_match not in MEETING_MATCH_MODES:
        # a typo must never silently widen what reaches the public calendar
        raise ValueError(f"event mapping {version} has an unknown meeting match mode {raw_match!r}")
    return EventTypeMapping(
        version=version,
        subcalendar_types=types,
        private_subcalendars=_ids(payload, "private_subcalendars"),
        excluded_subcalendars=_ids(payload, "excluded_subcalendars"),
        meeting_subcalendars=_ids(payload, "meeting_subcalendars"),
        rent_subcalendars=_ids(payload, "rent_subcalendars"),
        meeting_titles=titles or DEFAULT_MEETING_TITLES,
        meeting_label=(str(payload.get("meeting_label") or "").strip() or DEFAULT_MEETING_LABEL),
        meeting_match=raw_match,
    )


def unmapped_policy() -> ScopePolicy:
    """The explicit unconfirmed contour: nothing is known, nothing is zero."""

    return ScopePolicy(version=UNMAPPED_VERSION, contour_confirmed=False)


def policy_from_store(conn: sqlite3.Connection) -> ScopePolicy:
    loaded = repo.load_category_mapping(conn)
    if loaded is None:
        # no mapping recorded yet: the contour is unknown, money stays unknown
        return unmapped_policy()
    version, payload = loaded
    return scope_policy_from_payload(version, payload)


def event_mapping_from_store(conn: sqlite3.Connection) -> EventTypeMapping:
    loaded = repo.load_event_mapping(conn)
    if loaded is None:
        return EventTypeMapping(version=UNMAPPED_VERSION)
    version, payload = loaded
    return event_mapping_from_payload(version, payload)


def event_mapping_for(
    conn: sqlite3.Connection,
    *,
    config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> EventTypeMapping:
    """The calendar mapping of this cycle, in the documented precedence order.

    ``docs/CALENDAR.md`` section 4 fixes the order: the versioned row the
    editorial desk recorded in the store wins (loading never writes, so a desk
    edit is never overwritten); then a private mapping file; then the nightly
    cycle's stable-ID environment variables. With nothing configured the defaults
    apply and nothing is promoted to the public calendar by accident.
    """

    stored = event_mapping_from_store(conn)
    if stored.version != UNMAPPED_VERSION:
        return stored
    if config_path:
        return event_mapping_from_payload_file(config_path)
    return event_mapping_from_env(environ, version="env-config")


def read_mapping_file(path: str | Path) -> Mapping[str, Any]:
    """Read a private mapping payload; a configured but unusable file is refused."""

    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"finance policy file not found: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"finance policy file {target} is not a JSON mapping: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"finance policy file {target} must hold a JSON object")
    inner = payload.get("policy")
    if isinstance(inner, Mapping):
        payload = dict(inner, version=payload.get("version", inner.get("version")))
    return payload


def policy_from_payload_file(path: str | Path) -> ScopePolicy:
    payload = read_mapping_file(path)
    version = str(payload.get("version") or Path(path).stem)
    return scope_policy_from_payload(version, payload)


def event_mapping_from_payload_file(path: str | Path) -> EventTypeMapping:
    payload = read_mapping_file(path)
    version = str(payload.get("version") or Path(path).stem)
    return event_mapping_from_payload(version, payload)


def load_policy(conn: sqlite3.Connection, *, config_path: str | Path | None = None) -> ScopePolicy:
    """The confirmed map for this release: desk mapping first, then the private file."""

    loaded = repo.load_category_mapping(conn)
    if loaded is not None:
        # the newest recorded version is what the editorial desk decided; loading
        # is read-only, so a desk edit is never overwritten by the nightly cycle
        version, payload = loaded
        return scope_policy_from_payload(version, payload)
    if config_path:
        return policy_from_payload_file(config_path)
    return unmapped_policy()


__all__ = [
    "KNOWN_CLASSES",
    "KNOWN_EVENT_TYPES",
    "UNMAPPED_VERSION",
    "event_mapping_for",
    "event_mapping_from_payload",
    "event_mapping_from_payload_file",
    "event_mapping_from_store",
    "load_policy",
    "policy_from_payload_file",
    "policy_from_store",
    "read_mapping_file",
    "scope_policy_from_payload",
    "unmapped_policy",
]
