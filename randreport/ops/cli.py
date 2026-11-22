"""Command line entry point for the nightly cycle.

Wires the connectors from the process environment and runs one logical nightly
cycle. Secrets are read from the environment only; nothing here prints a token,
a calendar key or a raw payload. A source whose credentials are absent is skipped
and reported as not configured — the cycle still publishes the other sections.

    python -m randreport.ops.cli run [--period YYYY-MM] [--dry-run]
    python -m randreport.ops.cli status
    python -m randreport.ops.cli backup --db PATH --out BACKUP.sqlite
    python -m randreport.ops.cli restore --backup BACKUP.sqlite --db PATH [--force]
    python -m randreport.ops.cli verify-backup --backup BACKUP.sqlite

Exit codes: 0 published, 2 at least one source failed, 3 another run holds the
lease, 4 no source was configured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import leakguard
from ..connectors.teamup import TeamupClient
from ..connectors.telegram import MTProtoHistorySource, TelegramClient
from ..connectors.transport import UrllibTransport
from ..connectors.zenmoney import ZenmoneyClient
from ..domain.events import EventTypeMapping
from ..domain.finance import ScopePolicy
from ..domain.timeframes import month_period, parse_period, previous_month
from ..news.extract import NewsExtractor
from ..store import apply_migrations, connect
from .pipeline import ReleaseBuilder
from .run import (
    NewsAdapter,
    NightlyRunner,
    RunInProgress,
    SourceAdapter,
    TelegramAdapter,
    TeamupAdapter,
    ZenmoneyAdapter,
)
from .schedule import logical_date_for


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def _open_db(path: str):
    """Open (and migrate) the database; return None when it cannot be opened."""

    try:
        conn = connect(path)
        apply_migrations(conn)
        return conn
    except Exception:  # noqa: BLE001 - a bad path must not print a traceback with secrets
        return None


def build_adapters(conn: Any, *, logical_date: str) -> tuple[list[SourceAdapter], list[str], Any]:
    """Build the adapters whose credentials are present; return notes for the rest."""

    transport = UrllibTransport()
    adapters: list[SourceAdapter] = []
    notes: list[str] = []
    extractor: NewsExtractor | None = None

    zen_token = _env("ZENMONEY_TOKEN")
    if zen_token:
        client = ZenmoneyClient(
            transport,
            token=zen_token,
            base_url=_env("ZENMONEY_BASE_URL") or "https://api.zenmoney.ru",
        )
        adapters.append(ZenmoneyAdapter(client))
    else:
        notes.append("zenmoney: not configured (ZENMONEY_TOKEN missing)")

    calendar_key = _env("TEAMUP_CALENDAR_KEY")
    if calendar_key:
        client = TeamupClient(transport, calendar_key=calendar_key, token=_env("TEAMUP_TOKEN"))
        adapters.append(TeamupAdapter(client))
    else:
        notes.append("teamup: not configured (TEAMUP_CALENDAR_KEY missing)")

    channel = _env("TELEGRAM_PUBLIC_CHANNEL")
    bot_token = _env("TELEGRAM_BOT_TOKEN")
    if channel and bot_token:
        client = TelegramClient(
            transport,
            bot_token=bot_token,
            history=MTProtoHistorySource(allowed_channel_ids=(channel,)),
            allowed_channel_ids=(channel,),
        )
        adapters.append(TelegramAdapter(client, chat_id=channel, channel_id=channel))
        extractor = NewsExtractor(
            conn,
            provider=None,  # a provider is attached below only when configured
            channel_username=channel.lstrip("@"),
            max_calls_per_date=int(_env("NEWS_LLM_MAX_CALLS_PER_DAY", "50") or 50),
        )
        if _env("NEWS_LLM_PROVIDER", "none") not in (None, "", "none"):
            notes.append("news: provider configured; attach the provider implementation before use")
        adapters.append(NewsAdapter(extractor))
    else:
        notes.append("telegram: not configured (TELEGRAM_PUBLIC_CHANNEL / TELEGRAM_BOT_TOKEN missing)")

    return adapters, notes, extractor


def _policy_from_env() -> ScopePolicy:
    """A policy is required to publish finance; without one, money stays unknown."""

    return ScopePolicy(version="env-policy-unset")


def _maintenance(args: argparse.Namespace) -> int:
    """backup / restore / verify-backup: consistent copy with a verified restore."""

    from .backup import BackupError, backup_database, describe_database, restore_database

    try:
        if args.action == "backup":
            if not args.out:
                print("--out is required for action=backup", file=sys.stderr)
                return 2
            manifest = backup_database(args.db, args.out)
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            return 0
        if args.action == "restore":
            if not args.backup:
                print("--backup is required for action=restore", file=sys.stderr)
                return 2
            report = restore_database(args.backup, args.db, force=args.force)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["usable"] else 2
        target = args.backup or args.db
        report = describe_database(target)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["usable"] else 2
    except BackupError as exc:
        # a path or an integrity problem, never a secret or a payload
        print(f"maintenance failed: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    # the nightly cycle checks, before it exits, that it left no SQLite handle,
    # listening socket or HTTP response open (see randreport/leakguard.py)
    leakguard.install(verbose=False)

    parser = argparse.ArgumentParser(prog="randreport.ops.cli")
    parser.add_argument("action", choices=["run", "status", "backup", "restore", "verify-backup"])
    parser.add_argument("--period", default=None, help="публичный период YYYY-MM (по умолчанию — прошлый месяц МСК)")
    parser.add_argument("--dry-run", action="store_true", help="собрать релиз, но не переключать текущий")
    parser.add_argument("--db", default=_env("RANREPORT_DB", "/var/lib/randreport/randreport.sqlite"))
    parser.add_argument("--publish-dir", default=_env("RANREPORT_PUBLISH_DIR"))
    parser.add_argument("--lease", default=_env("RANREPORT_LEASE", "/var/lib/randreport/nightly.lock"))
    parser.add_argument("--out", default=None, help="целевой файл резервной копии (action=backup/restore)")
    parser.add_argument("--backup", default=None, help="файл резервной копии для восстановления (action=restore/verify-backup)")
    parser.add_argument("--force", action="store_true", help="разрешить восстановление поверх существующей БД")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.action in ("backup", "restore", "verify-backup"):
        return _maintenance(args)

    if args.action == "status":
        return _run_status(args)
    return _run_nightly(args)


def _run_status(args: argparse.Namespace) -> int:
    from .status import source_status

    conn = _open_db(args.db)
    if conn is None:
        print(f"cannot open the database at {args.db}", file=sys.stderr)
        return 5
    try:
        for source in ("zenmoney", "teamup", "telegram", "news"):
            status = source_status(conn, source)
            print(json.dumps(status.__dict__, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        conn.close()


def _run_nightly(args: argparse.Namespace) -> int:
    conn = _open_db(args.db)
    if conn is None:
        print(f"cannot open the database at {args.db}", file=sys.stderr)
        return 5
    try:
        return _nightly_with(conn, args)
    finally:
        conn.close()


def _nightly_with(conn: Any, args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    logical_date = logical_date_for(now)
    adapters, notes, _extractor = build_adapters(conn, logical_date=logical_date)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if not adapters:
        print("no source configured", file=sys.stderr)
        return 4

    period = parse_period(args.period) if args.period else month_period(previous_month(logical_date[:7]))
    builder = ReleaseBuilder(policy=_policy_from_env(), event_mapping=EventTypeMapping(version="env-unset"), period=period)
    runner = NightlyRunner(
        conn,
        adapters=adapters,
        builder=builder,
        publish_dir=args.publish_dir,
        lease_path=args.lease,
    )
    try:
        report = runner.run(now=now, publish=not args.dry_run)
    except RunInProgress as exc:
        print(f"run in progress: {exc}", file=sys.stderr)
        return 3
    except Exception:  # noqa: BLE001
        # a failure message never includes a payload, a token or a private id
        print("nightly run failed: " + traceback.format_exc().splitlines()[-1], file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "run_id": report.run_id,
                "release_id": report.release_id,
                "published": report.published,
                "freshness": report.freshness,
                "completeness": report.completeness,
                "sources": {name: {"status": out.status, "coverage": out.coverage} for name, out in report.sources.items()},
                "issues": list(report.issues),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    failed = [name for name, out in report.sources.items() if out.status == "failed"]
    return 2 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
