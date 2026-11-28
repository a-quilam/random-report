"""Command line entry point for the nightly cycle.

Wires the connectors from the process environment and runs one logical nightly
cycle. Secrets are read from the environment only; nothing here prints a token,
a calendar key or a raw payload. A source whose credentials are absent is skipped
and reported as not configured — the cycle still publishes the other sections.

The finance contour is *not* hardcoded: the confirmed account/category map is
loaded from the newest mapping the editorial desk recorded in the private store
(or, when the store has none, from a private mapping file). With no confirmed map
every money indicator stays unknown instead of zero. Loading is read-only, so a
desk edit is never overwritten by a nightly run.

    python -m randreport.ops.cli run [--period YYYY-MM[,YYYY...]] [--dry-run]
    python -m randreport.ops.cli status
    python -m randreport.ops.cli backup --db PATH --out BACKUP.sqlite [--encrypt]
    python -m randreport.ops.cli restore --backup BACKUP.sqlite --db PATH [--force]
    python -m randreport.ops.cli verify-backup --backup BACKUP.sqlite
    python -m randreport.ops.cli prune-backups --out BACKUP_DIR [--keep-daily 14 --keep-monthly 1]
    python -m randreport.ops.cli rollback --publish-dir DIR (--to REL_ID | --previous)
    python -m randreport.ops.cli reconcile --period YYYY-MM[,YYYY...] [--publish-dir DIR]

Without ``--period`` the cycle publishes every whole period that has data, the
current (still unfinished) month and the matching years — the year aggregate is
always the ratio of the year's sums, never the mean of monthly percentages.

Exit codes: 0 published, 2 at least one source failed, 3 another run holds the
lease, 4 no source was configured, 5 the database or the mapping is unusable.

``reconcile`` is a separate, read-only operator action: it opens the private
store without migrating or writing, reuses the frozen domain to recompute the
approved totals of the requested periods, and reports aggregate divergences plus
the reasons the figures are provisional. It prints aggregates and public
category keys only - never an operation, a transaction id or an account id.
Exit codes: 0 reconciled without an error, 1 an error-severity divergence, 2 a
bad period, 5 the store cannot be read.
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
from ..connectors.teamup import DEFAULT_BASE_URL as TEAMUP_DEFAULT_BASE_URL
from ..connectors.teamup import TeamupClient
from ..connectors.telegram import DEFAULT_BASE_URL as TELEGRAM_DEFAULT_BASE_URL
from ..connectors import telethon_stats
from ..connectors.telegram import MTProtoHistorySource, TelegramClient
from ..connectors.transport import UrllibTransport
from ..connectors.zenmoney import ZenmoneyClient
from ..domain.timeframes import Period, month_period, msk_day, parse_period, year_period
from ..inbox.import_json import ImportRejected, import_public_posts
from ..inbox.store import INBOX_SOURCE, inbox_counts
from ..news.extract import NewsExtractor
from ..news.provider import LlmConfigError, config_from_env, provider_from_config
from ..projection.policy import event_mapping_for, load_policy
from ..redaction import redact
from ..store import apply_migrations, connect
from ..store import repository as repo
from .pipeline import ReleaseBuilder, build_releases
from .run import (
    InboxAdapter,
    NewsAdapter,
    NightlyRunner,
    PolicyBalanceReader,
    RunInProgress,
    SourceAdapter,
    TelegramAdapter,
    TelegramStatsAdapter,
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


def build_adapters(conn: Any, *, logical_date: str, policy: Any = None) -> tuple[list[SourceAdapter], list[str], Any]:
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
        # balances are captured with an explicit liquidity, decided by the account
        # itself, never by the account merely belonging to the public contour
        reader = PolicyBalanceReader(policy) if policy is not None else None
        adapters.append(ZenmoneyAdapter(client, balance_reader=reader))
    else:
        notes.append("zenmoney: not configured (ZENMONEY_TOKEN missing)")

    calendar_key = _env("TEAMUP_CALENDAR_KEY")
    if calendar_key:
        client = TeamupClient(
            transport,
            calendar_key=calendar_key,
            # the internal identity is configured separately (a stable placeholder
            # id): rotating the secret key must not create a second calendar
            calendar_id=_env("TEAMUP_CALENDAR_ID") or None,
            token=_env("TEAMUP_TOKEN"),
            # the documented base URL is honoured, so a staging calendar (or a
            # loopback fixture) can be driven through the real nightly cycle
            base_url=_env("TEAMUP_BASE_URL") or TEAMUP_DEFAULT_BASE_URL,
        )
        adapters.append(TeamupAdapter(client))
    else:
        notes.append("teamup: not configured (TEAMUP_CALENDAR_KEY missing)")

    channel = _env("TELEGRAM_PUBLIC_CHANNEL")
    bot_token = _env("TELEGRAM_BOT_TOKEN")
    # a history pull needs an approved read-only session; without one the webhook
    # inbox is the intake and the news coverage is honestly partial.  The claim in
    # the environment is not taken on faith: while the packaged source is a
    # placeholder the cycle refuses to call the coverage complete.
    history_source: Any = None
    history_verified = False
    if channel and _env("TELEGRAM_HISTORY_VERIFIED") == "1":
        candidate = MTProtoHistorySource(allowed_channel_ids=(channel,))
        if getattr(candidate, "is_placeholder", True):
            notes.append(
                "history: TELEGRAM_HISTORY_VERIFIED=1 but the packaged read-only source is a"
                " placeholder (no approved session); the news coverage stays partial"
            )
        else:
            history_source = candidate
            history_verified = True
    if channel and bot_token:
        client = TelegramClient(
            transport,
            bot_token=bot_token,
            history=history_source,
            allowed_channel_ids=(channel,),
            base_url=_env("TELEGRAM_BASE_URL") or TELEGRAM_DEFAULT_BASE_URL,
        )
        # the subscriber snapshot is one source; the posts/news are another
        adapters.append(TelegramAdapter(client, chat_id=channel, channel_id=channel))
    else:
        notes.append("telegram: not configured (TELEGRAM_PUBLIC_CHANNEL / TELEGRAM_BOT_TOKEN missing)")

    if channel:
        # the durable inbox is local and needs no credential: a webhook delivery
        # or an imported export is honoured even when the Bot API is unreachable
        adapters.append(InboxAdapter(history_verified=history_verified))
        news_config = _news_config(notes)
        provider = None
        if news_config is not None:
            try:
                provider = provider_from_config(transport, news_config, api_key=_env("NEWS_LLM_API_KEY"))
            except LlmConfigError as exc:
                notes.append(f"news: provider misconfigured ({exc}); extracting nothing this run")
        if news_config is not None and news_config.enabled and not news_config.budget:
            notes.append(
                "news: a provider is selected but the daily budget is 0 "
                f"(NEWS_LLM_MAX_CALLS_PER_DAY={news_config.max_calls_per_day}); no paid call is made"
            )
        extractor = NewsExtractor(
            conn,
            provider=provider,
            channel_username=channel.lstrip("@"),
            timeout_s=news_config.timeout_s if news_config else 30.0,
            retries=news_config.retries if news_config else 2,
            max_calls_per_date=news_config.max_calls_per_day if news_config else 0,
            max_tokens_per_date=news_config.max_tokens_per_day if news_config else 0,
        )
        adapters.append(NewsAdapter(extractor, history_verified=history_verified))
        if provider is None:
            notes.append("news: no LLM provider configured; the chronicle is published with an explicit gap")
    else:
        notes.append("news: not configured (TELEGRAM_PUBLIC_CHANNEL missing)")

    # Forwarding counters (MTProto via a bot login, Telethon 1.x). The credential
    # names are checked, never their values: a machine without the secrets simply
    # keeps the honest empty ranking, as before.
    try:
        stats_config = telethon_stats.resolve_config()
    except telethon_stats.StatsConfigError as exc:
        notes.append(f"telegram_stats: not configured ({exc})")
    else:
        adapters.append(TelegramStatsAdapter(telethon_stats.TelethonForwardReader(stats_config)))

    return adapters, notes, extractor


def _news_config(notes: list[str]):
    """The adapter configuration; a bad value is reported, never guessed around."""

    try:
        return config_from_env()
    except LlmConfigError as exc:
        notes.append(f"news: invalid configuration ({exc}); the adapter stays off")
        return None


def _policy_for(conn: Any, *, config_path: str | None):
    """The confirmed public-contour map for this cycle (never a hardcoded default)."""

    return load_policy(conn, config_path=config_path)


def _periods_for(conn: Any, *, requested: Sequence[str], now: datetime) -> list[Period]:
    """The public periods of this cycle.

    With an explicit ``--period`` those labels are used verbatim.  Otherwise the
    cycle covers every *whole* month that has data, the matching years (whose
    coverage is the ratio of the year's sums) and the current unfinished month
    last, so the site can switch between all of them.
    """

    if requested:
        return [parse_period(spec) for spec in requested]

    current = msk_day(now).strftime("%Y-%m")
    months: set[str] = set(repo.movement_months(conn))
    months.discard(current)
    whole = sorted(month for month in months if month < current)
    years = sorted({month[:4] for month in months} | {current[:4]})
    return [month_period(month) for month in whole] + [year_period(year) for year in years] + [month_period(current)]


def _maintenance(args: argparse.Namespace) -> int:
    """backup / restore / verify-backup: consistent copy with a verified restore."""

    from . import backup as backup_module
    from .backup import (
        BackupError,
        backup_database,
        describe_database,
        prune_backups,
        restore_database,
    )

    passphrase = _env(backup_module.PASSPHRASE_ENV) if hasattr(args, "encrypt") else None
    try:
        if args.action == "backup":
            if not args.out:
                print("--out is required for action=backup", file=sys.stderr)
                return 2
            if getattr(args, "encrypt", False) and not passphrase:
                print(f"--encrypt needs {backup_module.PASSPHRASE_ENV} in the environment", file=sys.stderr)
                return 2
            manifest = backup_database(args.db, args.out, passphrase=passphrase if args.encrypt else None)
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            return 0
        if args.action == "restore":
            if not args.backup:
                print("--backup is required for action=restore", file=sys.stderr)
                return 2
            report = restore_database(args.backup, args.db, force=args.force, passphrase=passphrase)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["usable"] else 2
        if args.action == "prune-backups":
            report = prune_backups(
                args.out or args.db,
                keep_daily=int(getattr(args, "keep_daily", 14)),
                keep_monthly=int(getattr(args, "keep_monthly", 1)),
                dry_run=args.dry_run,
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        target = args.backup or args.db
        report = describe_database(target, passphrase=passphrase)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["usable"] else 2
    except BackupError as exc:
        # a path or an integrity problem, never a secret or a payload
        print(f"maintenance failed: {exc}", file=sys.stderr)
        return 2


def _rollback(args: argparse.Namespace) -> int:
    """Point the public release directory back at an existing, verified revision."""

    from .publish import PublishError, previous_published_release, rollback_release

    root = args.publish_dir
    if not root:
        print("a release directory is required (--publish-dir or RANREPORT_PUBLISH_DIR)", file=sys.stderr)
        return 2
    target = args.to
    if args.previous and not target:
        target = previous_published_release(root)
        if target is None:
            print("no earlier release to roll back to", file=sys.stderr)
            return 2
    if not target:
        print("give --to RELEASE_ID or --previous", file=sys.stderr)
        return 2
    try:
        if args.dry_run:
            from ..web.release_index import ReleaseIndex

            if ReleaseIndex(root).get(target) is None:
                print(f"release {target!r} is not present or failed the allowlist", file=sys.stderr)
                return 2
            print(json.dumps({"release_id": target, "dry_run": True, "verified": True}, ensure_ascii=False))
            return 0
        report = rollback_release(root, target)
    except PublishError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["verified"] else 2


def _reconcile(args: argparse.Namespace) -> int:
    """Read-only operator reconciliation of the approved totals (ops.reconcile).

    The store is opened ``mode=ro`` and is never migrated or written; the periods
    are required (an operator reconciles named periods, not an implicit set), and
    the output is aggregate-only: public metric names, public category keys and
    the provisionality labels.
    """

    from .reconcile import ReconcileError, open_store, reconcile

    requested = [spec for spec in (args.period or "").split(",") if spec.strip()]
    if not requested:
        print("reconcile needs --period YYYY-MM[,YYYY...]", file=sys.stderr)
        return 2
    try:
        periods = [parse_period(spec.strip()) for spec in requested]
    except ValueError as exc:
        print(f"unsupported period: {exc}", file=sys.stderr)
        return 2

    try:
        conn = open_store(args.db)
    except ReconcileError as exc:
        print(f"reconcile refused: {exc}", file=sys.stderr)
        return 5
    try:
        reports = [
            reconcile(conn, period=period, release_dir=args.publish_dir).to_public()
            for period in periods
        ]
    except ReconcileError as exc:  # pragma: no cover - defensive; open_store is the gate
        print(f"reconcile refused: {exc}", file=sys.stderr)
        return 5
    except (FileNotFoundError, ValueError) as exc:
        # an unusable mapping is an operator error, never a payload dump
        print(f"cannot load the finance policy: {exc}", file=sys.stderr)
        return 5
    finally:
        conn.close()

    divergences = [d for report in reports for d in report["divergences"]]
    print(
        json.dumps(
            {"periods": reports, "divergence_count": len(divergences)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if any(d["severity"] == "error" for d in divergences) else 0


def main(argv: Sequence[str] | None = None) -> int:
    # the nightly cycle checks, before it exits, that it left no SQLite handle,
    # listening socket or HTTP response open (see randreport/leakguard.py)
    leakguard.install(verbose=False)

    parser = argparse.ArgumentParser(prog="randreport.ops.cli")
    parser.add_argument(
        "action",
        choices=[
            "run",
            "status",
            "backup",
            "restore",
            "verify-backup",
            "import-news",
            "webhook",
            "getupdates",
            "forwards",
            "prune-backups",
            "rollback",
            "reconcile",
        ],
    )
    parser.add_argument(
        "--period",
        default=None,
        help="публичные периоды через запятую: YYYY-MM, YYYY (по умолчанию — все целые периоды с данными, текущий неполный месяц и годы)",
    )
    parser.add_argument("--dry-run", action="store_true", help="собрать релиз, но не переключать текущий")
    parser.add_argument("--db", default=_env("RANREPORT_DB", "/var/lib/randreport/randreport.sqlite"))
    parser.add_argument("--publish-dir", default=_env("RANREPORT_PUBLISH_DIR"))
    parser.add_argument("--lease", default=_env("RANREPORT_LEASE", "/var/lib/randreport/nightly.lock"))
    parser.add_argument(
        "--policy",
        default=_env("RANREPORT_FINANCE_POLICY"),
        help="приватный файл карты публичного контура (используется, пока в БД нет записанной версии)",
    )
    parser.add_argument(
        "--events",
        default=_env("RANREPORT_EVENT_MAPPING"),
        help="приватный файл карты календаря (используется, пока в БД нет записанной версии стола; иначе — переменные TEAMUP_*)",
    )
    parser.add_argument("--out", default=None, help="целевой файл резервной копии (action=backup/restore)")
    parser.add_argument("--backup", default=None, help="файл резервной копии для восстановления (action=restore/verify-backup)")
    parser.add_argument("--force", action="store_true", help="разрешить восстановление поверх существующей БД")
    parser.add_argument("--file", default=None, help="публичный JSON-экспорт для импорта (action=import-news)")
    parser.add_argument("--channel", default=None, help="канал импорта (action=import-news; иначе TELEGRAM_PUBLIC_CHANNEL)")
    parser.add_argument("--source", default=None, help="метка источника импорта (action=import-news)")
    parser.add_argument("--host", default=_env("RANREPORT_WEBHOOK_HOST", "127.0.0.1"), help="адрес webhook (action=webhook)")
    parser.add_argument("--port", type=int, default=int(_env("RANREPORT_WEBHOOK_PORT", "8081") or 8081), help="порт webhook (action=webhook)")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="сколько обновлений забрать за один getUpdates (action=getupdates)",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="ширина скользящего окна измерения пересылок в днях (action=forwards)",
    )
    parser.add_argument(
        "--encrypt",
        action="store_true",
        help="записать резервную копию зашифрованной (пароль из RANREPORT_BACKUP_PASSPHRASE)",
    )
    parser.add_argument("--keep-daily", type=int, default=14, help="сколько дневных копий хранить (action=prune-backups)")
    parser.add_argument("--keep-monthly", type=int, default=1, help="сколько месячных копий хранить (action=prune-backups)")
    parser.add_argument("--to", default=None, help="идентификатор релиза для отката (action=rollback)")
    parser.add_argument("--previous", action="store_true", help="откатить на предыдущий релиз (action=rollback)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.action == "rollback":
        return _rollback(args)
    if args.action == "reconcile":
        return _reconcile(args)
    if args.action in ("backup", "restore", "verify-backup", "prune-backups"):
        return _maintenance(args)

    if args.action == "status":
        return _run_status(args)
    if args.action == "import-news":
        return _run_import(args)
    if args.action == "webhook":
        return _run_webhook(args)
    if args.action == "getupdates":
        return _run_getupdates(args)
    if args.action == "forwards":
        return _run_forwards(args)
    return _run_nightly(args)


def _run_status(args: argparse.Namespace) -> int:
    from .status import source_status

    conn = _open_db(args.db)
    if conn is None:
        print(f"cannot open the database at {args.db}", file=sys.stderr)
        return 5
    try:
        for source in ("zenmoney", "teamup", "telegram", INBOX_SOURCE, "news", "telegram_stats"):
            status = source_status(conn, source)
            print(json.dumps(status.__dict__, ensure_ascii=False, sort_keys=True))
        print(json.dumps({"source": "inbox_counts", **inbox_counts(conn)}, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        conn.close()


def _run_import(args: argparse.Namespace) -> int:
    """Import a missed public post export into the archive (§8 backfill).

    This writes private archive rows only: no money fact is touched and no public
    page changes until the next nightly cycle builds a release.
    """

    if not args.file:
        print("--file is required for action=import-news", file=sys.stderr)
        return 2
    channel = args.channel or _env("TELEGRAM_PUBLIC_CHANNEL")
    if not channel:
        print("--channel or TELEGRAM_PUBLIC_CHANNEL is required for action=import-news", file=sys.stderr)
        return 2
    try:
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read the export: {exc}", file=sys.stderr)
        return 2
    conn = _open_db(args.db)
    if conn is None:
        print(f"cannot open the database at {args.db}", file=sys.stderr)
        return 5
    try:
        report = import_public_posts(conn, payload, channel_id=channel, source_label=args.source or "public_export")
    except ImportRejected as exc:
        print(f"import rejected: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def build_webhook_app(db_path: str, *, env: Mapping[str, str] | None = None):
    """The webhook application, or ``None`` with a note when it must not start."""

    from ..inbox.webhook import TelegramWebhook, WebhookConfigError

    source = os.environ if env is None else env
    channels = [part.strip() for part in (source.get("TELEGRAM_PUBLIC_CHANNEL") or "").split(",") if part.strip()]
    try:
        return TelegramWebhook(
            secret_token=source.get("TELEGRAM_WEBHOOK_SECRET"),
            allowed_channels=channels,
            db_path=db_path,
            max_body_bytes=int(source.get("TELEGRAM_WEBHOOK_MAX_BODY_BYTES") or 16384),
        )
    except (WebhookConfigError, ValueError) as exc:
        print(f"webhook not started: {exc}", file=sys.stderr)
        return None


def _run_webhook(args: argparse.Namespace) -> int:
    """Serve the passive inbox.  The webhook is served, never registered: no
    ``setWebhook`` call and no outbound request leaves this process."""

    from wsgiref.simple_server import make_server

    from ..inbox.webhook import WEBHOOK_PATH

    app = build_webhook_app(args.db)
    if app is None:
        return 2
    with make_server(args.host, args.port, app) as httpd:
        print(
            json.dumps(
                {"serving": True, "host": args.host, "port": httpd.server_port, "path": WEBHOOK_PATH},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - operator interrupt
            pass
    return 0


def _run_getupdates(args: argparse.Namespace) -> int:
    """Drain the pending public posts once, without a webhook.

    The webhook is the other intake; when none is registered (checked through
    ``getWebhookInfo``) this short poll is the domain-less equivalent.  It never
    registers nor deletes a webhook, and only the allowed channel's posts and
    edits are stored (durably, before the offset moves).
    """

    from ..connectors.transport import UrllibTransport
    from ..inbox.polling import PollingError, WebhookActive, poll_into_inbox

    token = _env("TELEGRAM_BOT_TOKEN")
    channels = [part.strip() for part in (_env("TELEGRAM_PUBLIC_CHANNEL") or "").split(",") if part.strip()]
    if not token or not channels:
        print(
            "getupdates needs TELEGRAM_BOT_TOKEN and TELEGRAM_PUBLIC_CHANNEL",
            file=sys.stderr,
        )
        return 2
    conn = _open_db(args.db)
    if conn is None:
        print(f"cannot open the database at {args.db}", file=sys.stderr)
        return 5
    try:
        try:
            report = poll_into_inbox(
                conn,
                UrllibTransport(),
                bot_token=token,
                allowed_channels=channels,
                base_url=_env("TELEGRAM_BASE_URL") or "https://api.telegram.org",
                limit=args.limit,
            )
        except WebhookActive as exc:
            print(f"getupdates refused: {exc}", file=sys.stderr)
            return 2
        except PollingError as exc:
            print(f"getupdates failed: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def _run_forwards(args: argparse.Namespace) -> int:
    """One manual measurement pass of the Telegram forwarding counters.

    The same adapter the nightly cycle uses; the ids come from the archive's
    rolling window (``--window-days``, 30 by default).  The output is aggregate
    counts only — no counter value per post, no credential, nothing a secret
    could ride on.  Exit codes: 0 measured, 2 not configured or unavailable.
    """

    if args.window_days < 1:
        print("--window-days must be positive", file=sys.stderr)
        return 2
    conn = _open_db(args.db)
    if conn is None:
        print(f"cannot open the database at {args.db}", file=sys.stderr)
        return 5
    try:
        try:
            config = telethon_stats.resolve_config()
        except telethon_stats.StatsConfigError as exc:
            print(f"telegram_stats: not configured ({exc})", file=sys.stderr)
            return 2
        adapter = TelegramStatsAdapter(
            telethon_stats.TelethonForwardReader(config), window_days=args.window_days
        )
        try:
            outcome = adapter.sync(
                conn=conn,
                cutoff=datetime.now(timezone.utc),
                logical_date=logical_date_for(datetime.now(timezone.utc)),
                cursor=None,
            )
        except telethon_stats.StatsUnavailable as exc:
            print(f"telegram_stats: unavailable ({exc})", file=sys.stderr)
            return 2
        print(json.dumps({"source": adapter.name, **outcome.payload}, ensure_ascii=False, sort_keys=True))
        return 0 if outcome.status == "ok" else 2
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

    try:
        policy = _policy_for(conn, config_path=args.policy)
    except (FileNotFoundError, ValueError) as exc:
        # a configured but unusable map is an operator error, never a payload dump
        print(f"cannot load the finance policy: {exc}", file=sys.stderr)
        return 5
    if not policy.contour_confirmed:
        print(
            "note: no confirmed public-contour map is recorded; finance stays unknown "
            "(the editorial desk records it, or --policy names a private mapping file)",
            file=sys.stderr,
        )

    adapters, notes, _extractor = build_adapters(conn, logical_date=logical_date, policy=policy)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if not adapters:
        print("no source configured", file=sys.stderr)
        return 4

    requested = [spec for spec in (args.period or "").split(",") if spec]
    try:
        periods = _periods_for(conn, requested=requested, now=now)
    except ValueError as exc:
        print(f"unsupported period: {exc}", file=sys.stderr)
        return 2
    if not periods:  # pragma: no cover - defensive: the current month is always present
        print("no public period to publish", file=sys.stderr)
        return 2

    try:
        mapping = event_mapping_for(conn, config_path=args.events)
    except (FileNotFoundError, ValueError) as exc:
        print(f"cannot load the event mapping: {exc}", file=sys.stderr)
        return 5

    builder = ReleaseBuilder(policy=policy, event_mapping=mapping, period=periods[-1])
    runner = NightlyRunner(
        conn,
        adapters=adapters,
        builder=builder,
        publish_dir=args.publish_dir,
        lease_path=args.lease,
    )
    try:
        # the sources are pulled once; publication happens below, per period, so a
        # single cycle serves every whole period and the current unfinished one
        report = runner.run(now=now, publish=False)
    except RunInProgress as exc:
        print(f"run in progress: {exc}", file=sys.stderr)
        return 3
    except Exception:  # noqa: BLE001
        # a failure message never includes a payload, a token or a private id: the
        # last line is redacted before it reaches stderr (randreport/redaction.py)
        print("nightly run failed: " + redact(traceback.format_exc().splitlines()[-1]), file=sys.stderr)
        return 2

    releases: list[tuple[str, str]] = []
    if not args.dry_run:
        try:
            # the periods are resolved *after* the sync: a fresh store has no facts
            # to enumerate, and the cycle must publish what it has just read
            periods = _periods_for(conn, requested=requested, now=now)
            releases = build_releases(
                conn,
                policy=policy,
                event_mapping=mapping,
                periods=periods,
                release_dir=args.publish_dir,
                now=now,
                freshness=report.freshness,
                completeness=report.completeness,  # kept for operators; sections carry their own
                source_labels=tuple(adapter.name for adapter in adapters),
                finance_data_present=_finance_present(conn, report),
            )
        except Exception:  # noqa: BLE001 - never publish an unsafe revision
            print("nightly run failed: " + traceback.format_exc().splitlines()[-1], file=sys.stderr)
            return 2
        repo.audit(
            conn,
            action="release_published",
            entity_type="release",
            entity_key=releases[-1][1] if releases else None,
            detail={"periods": [label for label, _ in releases], "policy_version": policy.version},
            now=now.isoformat(),
        )

    print(
        json.dumps(
            {
                "run_id": report.run_id,
                "logical_date": report.logical_date,
                "release_id": releases[-1][1] if releases else report.release_id,
                "published": bool(releases),
                "releases": {label: release_id for label, release_id in releases},
                "freshness": report.freshness,
                "completeness": report.completeness,
                "policy_version": policy.version,
                "contour_confirmed": policy.contour_confirmed,
                "sources": {name: {"status": out.status, "coverage": out.coverage} for name, out in report.sources.items()},
                "issues": list(report.issues),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    failed = [name for name, out in report.sources.items() if out.status == "failed"]
    return 2 if failed else 0


def _finance_present(conn: Any, report: Any) -> bool:
    """The finance source was read in this cycle (or facts exist): never assume 0."""

    zenmoney = report.sources.get("zenmoney")
    if zenmoney is not None and zenmoney.status == "ok":
        return True
    return bool(repo.load_movements(conn))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
