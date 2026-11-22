"""Consistent backup, restore and verification of the private store.

The nightly cycle, the desk and the public site all share one SQLite database.
A backup is therefore taken with the SQLite **backup API**, never by copying the
file: the database runs in WAL mode, and copying a live ``.sqlite`` leaves the
``-wal`` sidecar behind, which yields a torn or empty restore
(docs/REQUIREMENTS.md section 9).

A restore is written to a *clean* target path and refuses to overwrite an
existing database without an explicit ``force``; the result is verified with
``PRAGMA integrity_check`` and against the required-table list before it is
reported as usable. Nothing here prints a secret: the store holds no tokens.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..store import MIGRATIONS, REQUIRED_TABLES, connect, missing_tables, schema_version

# Tables whose contents prove a restore kept the frozen history, not just a schema.
WITNESS_TABLES = (
    "report_snapshots",
    "finance_transactions",
    "calendar_occurrences",
    "news_items",
    "news_revisions",
    "source_cursors",
    "category_mapping_versions",
    "event_type_mapping",
    "admin_members",
    "admin_sessions",
    "audit_log",
    "checks",
    "historical_observations",
    "public_releases",
)


class BackupError(RuntimeError):
    """A backup or restore that must not be reported as successful."""


def _sidecars(path: Path) -> tuple[Path, Path]:
    return (path.with_suffix(path.suffix + "-wal"), path.with_suffix(path.suffix + "-shm"))


def _drop_sidecars(path: Path) -> None:
    """Remove an empty WAL/SHM sidecar so a backup is a single file.

    A non-empty ``-wal`` is never deleted: that would discard the very writes the
    requirement warns about (docs/REQUIREMENTS.md section 9).
    """

    for sidecar in _sidecars(path):
        try:
            if sidecar.exists() and sidecar.stat().st_size == 0:
                sidecar.unlink()
        except OSError:  # pragma: no cover - a locked sidecar is left in place
            continue


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    present = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return {
        name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
        for name in WITNESS_TABLES
        if name in present
    }


def describe_database(path: str | Path) -> dict[str, Any]:
    """Integrity, schema version and witness row counts of an existing database."""

    target = Path(path)
    if not target.exists():
        raise BackupError(f"database does not exist: {target}")
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        found_missing = sorted(missing_tables(conn))
        return {
            "path": str(target),
            "integrity": integrity,
            "schema_version": schema_version(conn),
            "expected_schema_version": len(MIGRATIONS),
            "missing_tables": found_missing,
            "row_counts": _counts(conn),
            "usable": integrity == "ok" and not found_missing,
        }
    finally:
        conn.close()


def backup_database(source: str | Path, target: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Write a consistent copy of ``source`` to ``target`` and return a manifest."""

    src_path = Path(source)
    dst_path = Path(target)
    if not src_path.exists():
        raise BackupError(f"source database does not exist: {src_path}")
    if dst_path.exists():
        raise BackupError(f"refusing to overwrite an existing backup: {dst_path}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    src = connect(src_path)  # opens WAL correctly and never copies the sidecar
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)  # the documented consistent SQLite backup API
        dst.commit()
        # The source is in WAL mode, so the copy starts out in WAL mode too.
        # Switched back to a rollback journal, the backup is one self-contained
        # file: it can be copied to another host with no ``-wal`` sidecar to lose.
        dst.execute("PRAGMA journal_mode = DELETE")
        integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise BackupError(f"backup failed its integrity check: {integrity}")
        dst.row_factory = sqlite3.Row
        row_counts = _counts(dst)
        version = schema_version(dst)
    finally:
        dst.close()
        src.close()
    _drop_sidecars(dst_path)

    manifest = {
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "source": str(src_path),
        "backup": str(dst_path),
        "bytes": dst_path.stat().st_size,
        "integrity": "ok",
        "schema_version": version,
        "row_counts": row_counts,
    }
    manifest_path = dst_path.with_suffix(dst_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def restore_database(backup: str | Path, target: str | Path, *, force: bool = False) -> dict[str, Any]:
    """Restore a backup onto a *clean* target path and verify the result."""

    src_path = Path(backup)
    dst_path = Path(target)
    if not src_path.exists():
        raise BackupError(f"backup does not exist: {src_path}")
    if dst_path.exists() and not force:
        raise BackupError(f"target already exists, refusing to overwrite without force: {dst_path}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = dst_path.with_suffix(dst_path.suffix + ".restoring")
    if temp_path.exists():
        temp_path.unlink()

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(temp_path))
    try:
        src.backup(dst)  # a copy of the backup, not of the file system entry
        dst.commit()
        integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        dst.close()
        src.close()
    if integrity != "ok":
        temp_path.unlink(missing_ok=True)
        raise BackupError(f"restored copy failed its integrity check: {integrity}")
    os.replace(temp_path, dst_path)
    # a restored database must never carry a stale WAL next to it
    _drop_sidecars(dst_path)
    return describe_database(dst_path)
