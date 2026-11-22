"""Consistent backup, encryption, retention, restore and verification of the store.

The nightly cycle, the desk and the public site all share one SQLite database.
A backup is therefore taken with the SQLite **backup API**, never by copying the
file: the database runs in WAL mode, and copying a live ``.sqlite`` leaves the
``-wal`` sidecar behind, which yields a torn or empty restore
(docs/REQUIREMENTS.md section 9).

A restore is written to a *clean* target path and refuses to overwrite an
existing database without an explicit ``force``; the result is verified with
``PRAGMA integrity_check`` and against the required-table list before it is
reported as usable. Nothing here prints a secret: the store holds no tokens.

Encryption. A backup can be written encrypted with AES-256-CBC/PBKDF2 through the
system ``openssl``; the passphrase is passed to ``openssl`` in its environment,
never in an argument (an argument is visible in ``ps``). An encrypted backup is
named ``*.enc`` so the format is self-describing, and restoring one without a
passphrase is refused rather than silently producing garbage.

Retention. :func:`prune_backups` enforces the documented policy (14 daily copies
plus one monthly copy) and is written so that it can never delete the only local
copy of the history: with a single backup it deletes nothing, and it always keeps
the newest copy (docs/REQUIREMENTS.md section 9).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

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

CIPHER = "aes-256-cbc/pbkdf2"
PBKDF2_ITERATIONS = 200_000
PASSPHRASE_ENV = "RANREPORT_BACKUP_PASSPHRASE"

#: Suffix that marks a file as an encrypted backup.
ENCRYPTED_SUFFIXES = (".enc",)
#: Suffixes a backup file may carry (used by retention).
BACKUP_SUFFIXES = (".sqlite", ".db", ".enc")


class BackupError(RuntimeError):
    """A backup or restore that must not be reported as successful."""


# --------------------------------------------------------------------------- #
# consistent copy
# --------------------------------------------------------------------------- #

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


def _write_consistent_copy(src_path: Path, dst_path: Path) -> dict[str, Any]:
    """SQLite backup API copy of ``src_path`` into the (non-existing) ``dst_path``."""

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
    return {"integrity": "ok", "schema_version": version, "row_counts": row_counts}


def describe_database(path: str | Path, *, passphrase: str | None = None) -> dict[str, Any]:
    """Integrity, schema version and witness row counts of a backup or database.

    An encrypted backup (``*.enc``) is decrypted into a private temporary
    directory first: opening the ciphertext as SQLite only ever yields
    "file is not a database", so the passphrase is required to verify one — and a
    missing one is refused in words rather than as a driver error.
    """

    target = Path(path)
    if not target.exists():
        raise BackupError(f"database does not exist: {target}")
    if _is_encrypted(target):
        if not passphrase:
            raise BackupError("this backup is encrypted; a passphrase is required to verify it")
        with tempfile.TemporaryDirectory(prefix="randreport-verify-") as work:
            staged = Path(work) / "decrypted.sqlite"
            decrypt_file(target, staged, passphrase=passphrase)
            report = describe_database(staged)
        report["path"] = str(target)
        report["encrypted"] = True
        return report
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"not a readable database: {target} ({exc})") from exc
    conn.row_factory = sqlite3.Row
    try:
        try:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            # a truncated or unrelated file: a diagnosis, never a traceback
            raise BackupError(f"not a readable database: {target} ({exc})") from exc
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


# --------------------------------------------------------------------------- #
# encryption (openssl, passphrase via the child environment)
# --------------------------------------------------------------------------- #

def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def _run_openssl(args: Sequence[str], passphrase: str) -> None:
    env = dict(os.environ)
    # the passphrase travels in the child environment, never on the command line
    env[PASSPHRASE_ENV] = passphrase
    try:
        completed = subprocess.run(
            ["openssl", *args, "-pass", f"env:{PASSPHRASE_ENV}"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - openssl is present here
        raise BackupError("openssl is required for an encrypted backup") from exc
    if completed.returncode != 0:
        # never echo the passphrase or openssl's argv; the exit code is enough
        raise BackupError(f"openssl failed with exit code {completed.returncode}")


def encrypt_file(source: str | Path, target: str | Path, *, passphrase: str) -> None:
    """Encrypt ``source`` to ``target`` with AES-256-CBC/PBKDF2."""

    if not passphrase:
        raise BackupError("an empty passphrase is not allowed")
    if not openssl_available():
        raise BackupError("openssl is required for an encrypted backup")
    _run_openssl(
        [
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            str(PBKDF2_ITERATIONS),
            "-salt",
            "-in",
            str(source),
            "-out",
            str(target),
        ],
        passphrase,
    )


def decrypt_file(source: str | Path, target: str | Path, *, passphrase: str) -> None:
    """Decrypt an ``openssl enc`` backup; a wrong passphrase raises."""

    if not passphrase:
        raise BackupError("a passphrase is required to restore an encrypted backup")
    if not openssl_available():
        raise BackupError("openssl is required to restore an encrypted backup")
    _run_openssl(
        [
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            str(PBKDF2_ITERATIONS),
            "-in",
            str(source),
            "-out",
            str(target),
        ],
        passphrase,
    )


def _is_encrypted(path: Path) -> bool:
    return path.suffix.lower() in ENCRYPTED_SUFFIXES


# --------------------------------------------------------------------------- #
# backup / restore
# --------------------------------------------------------------------------- #

def backup_database(
    source: str | Path,
    target: str | Path,
    *,
    now: datetime | None = None,
    passphrase: str | None = None,
) -> dict[str, Any]:
    """Write a consistent copy of ``source`` to ``target`` and return a manifest.

    With a ``passphrase`` the copy is written encrypted to ``*.enc`` and the
    plaintext never persists outside a private temporary directory.
    """

    src_path = Path(source)
    dst_path = Path(target)
    if not src_path.exists():
        raise BackupError(f"source database does not exist: {src_path}")
    if dst_path.exists():
        raise BackupError(f"refusing to overwrite an existing backup: {dst_path}")
    encrypted = bool(passphrase)
    if encrypted and not _is_encrypted(dst_path):
        raise BackupError("an encrypted backup must be written to a path ending in .enc")
    if encrypted and not openssl_available():
        raise BackupError("openssl is required for an encrypted backup")
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    descriptor: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="randreport-backup-") as work:
        plain_path = Path(work) / "randreport.sqlite"
        descriptor = _write_consistent_copy(src_path, plain_path)
        if encrypted:
            assert passphrase is not None
            encrypt_file(plain_path, dst_path, passphrase=passphrase)
        else:
            os.replace(plain_path, dst_path)
        # the whole temp dir, including a plaintext copy, is removed with it

    manifest = {
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "source": str(src_path),
        "backup": str(dst_path),
        "bytes": dst_path.stat().st_size,
        "integrity": descriptor["integrity"],
        "schema_version": descriptor["schema_version"],
        "row_counts": descriptor["row_counts"],
        "encrypted": encrypted,
        "cipher": CIPHER if encrypted else None,
    }
    manifest_path = dst_path.with_suffix(dst_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def restore_database(
    backup: str | Path,
    target: str | Path,
    *,
    force: bool = False,
    passphrase: str | None = None,
) -> dict[str, Any]:
    """Restore a backup onto a *clean* target path and verify the result.

    An encrypted backup (``*.enc``) is decrypted inside a private temporary
    directory first; a missing or wrong passphrase fails the restore instead of
    writing a corrupt database over the target.
    """

    src_path = Path(backup)
    dst_path = Path(target)
    if not src_path.exists():
        raise BackupError(f"backup does not exist: {src_path}")
    if dst_path.exists() and not force:
        raise BackupError(f"target already exists, refusing to overwrite without force: {dst_path}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="randreport-restore-") as work:
        staged_backup = src_path
        if _is_encrypted(src_path):
            if not passphrase:
                raise BackupError("this backup is encrypted; a passphrase is required to restore it")
            staged_backup = Path(work) / "decrypted.sqlite"
            decrypt_file(src_path, staged_backup, passphrase=passphrase)

        temp_path = dst_path.with_suffix(dst_path.suffix + ".restoring")
        if temp_path.exists():
            temp_path.unlink()

        src = sqlite3.connect(f"file:{staged_backup}?mode=ro", uri=True)
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


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #

def find_backups(directory: str | Path) -> tuple[Path, ...]:
    """Every backup file in ``directory``, newest first (by mtime, then name)."""

    base = Path(directory)
    if not base.is_dir():
        return ()
    found = [
        entry
        for entry in base.iterdir()
        if entry.is_file()
        and not entry.name.endswith(".manifest.json")
        and any(entry.name.endswith(suffix) for suffix in BACKUP_SUFFIXES)
    ]
    return tuple(sorted(found, key=lambda p: (p.stat().st_mtime, p.name), reverse=True))


def prune_backups(
    directory: str | Path,
    *,
    keep_daily: int = 14,
    keep_monthly: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply the documented retention policy, never deleting the only copy.

    The newest backup is always kept, the newest ``keep_daily`` are kept, and for
    each of the last ``keep_monthly`` calendar months the newest copy is kept so a
    month can always be restored. Only then are the remaining, older copies
    removed -- and only when at least one copy would survive (a single backup is
    never deleted, section 9).
    """

    backups = find_backups(directory)
    if len(backups) <= 1:
        return {"kept": [str(p) for p in backups], "deleted": [], "reason": "single_copy"}

    keep: set[Path] = set(backups[: max(1, keep_daily)])
    keep.add(backups[0])  # the newest is always kept

    monthly: dict[str, Path] = {}
    for path in backups:
        month = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m")
        monthly.setdefault(month, path)  # newest-first, so the first seen is the newest of the month
    for month in sorted(monthly, reverse=True)[: max(0, keep_monthly)]:
        keep.add(monthly[month])

    candidates = [p for p in backups if p not in keep]
    if len(backups) - len(candidates) < 1:  # pragma: no cover - keep is never empty here
        return {"kept": [str(p) for p in backups], "deleted": [], "reason": "would_delete_last_copy"}

    deleted: list[Path] = []
    for path in candidates:
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        if not dry_run:
            path.unlink()
            if manifest.exists():
                manifest.unlink()
        deleted.append(path)
    return {
        "kept": [str(p) for p in sorted(keep, key=lambda p: p.name)],
        "deleted": [str(p) for p in deleted],
        "reason": "retention",
    }
