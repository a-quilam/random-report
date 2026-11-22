"""Build public releases from the private store (the web side of the pipeline).

This mirrors what the nightly cycle does, but exposes it as a callable so a period
can be published on demand (for example while a month is being closed in the
editorial desk). It only reads the private store and calls the frozen domain; it
never touches a source. The scope policy and the calendar mapping are loaded from
the versioned mapping rows (``randreport.projection.policy``), so a release is
always built with an explicit, traceable mapping version — the same loader the
nightly CLI uses.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..domain.events import EventTypeMapping
from ..domain.finance import ScopePolicy
from ..domain.timeframes import Period
from ..ops.pipeline import build_releases
from ..projection.policy import (
    event_mapping_for,
    event_mapping_from_payload,
    event_mapping_from_store,
    policy_from_store,
    scope_policy_from_payload,
)

# re-exported for the editorial desk and the diagnostics screen
__all__ = [
    "build_and_publish",
    "event_mapping_from_payload",
    "event_mapping_from_store",
    "policy_from_store",
    "scope_policy_from_payload",
]


def build_and_publish(
    conn: sqlite3.Connection,
    *,
    release_dir: str | Path | None,
    periods: Sequence[Period],
    now: datetime | None = None,
    freshness: str = "fresh",
    verification: str = "operational",
    source_labels: Sequence[str] = (),
    finance_data_present: bool = True,
) -> list[tuple[str, str]]:
    """Build one release per period and publish each atomically. Returns (period, release_id)."""

    stamp = now or datetime.now(timezone.utc)
    policy: ScopePolicy = policy_from_store(conn)
    # the same documented precedence as the nightly cycle: the desk's versioned
    # row, then a private file, then the stable-ID environment configuration
    mapping: EventTypeMapping = event_mapping_for(conn)
    return build_releases(
        conn,
        policy=policy,
        event_mapping=mapping,
        periods=periods,
        release_dir=release_dir,
        now=stamp,
        freshness=freshness,
        verification=verification,
        source_labels=tuple(source_labels),
        finance_data_present=finance_data_present,
    )


def release_files(release: Any, conn: sqlite3.Connection | None) -> dict[str, str]:
    """The files of one release, including the allowlisted report archive.

    The archive is what lets the public site render ``/reports/*`` without any
    access to the private database: it is produced here, from the private store,
    and published atomically with the release. With no connection (a build that
    did not read the store) the release simply carries no archive.
    """

    from . import reports as reports_module

    files = release.files()
    if conn is not None:
        archive = reports_module.public_archive(conn)
        files = {**files, "reports.json": json.dumps(archive, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}
    return files
