"""Command line entry point for the closed editorial desk.

    python -m randreport.admin serve      [--host 127.0.0.1 --port 8098]
    python -m randreport.admin bootstrap  --db PATH --telegram-id ID
    python -m randreport.admin member     --db PATH [--add ID | --remove ID]
    python -m randreport.admin sessions   --db PATH [--revoke ID]
    python -m randreport.admin history    --db PATH          # ingest observations + conflicts
    python -m randreport.admin diagnose   --db PATH          # collect findings, sync issues
    python -m randreport.admin close      --db PATH --period YYYY-MM --reason "..."
    python -m randreport.admin post       --db PATH --period YYYY-MM [--comment "..."]

Secrets (the server secret, the OIDC client id, the widget bot token) come only
from the environment; nothing here prints a secret.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .. import leakguard
from ..store import apply_migrations, connect
from ..store import repository as repo
from . import desk, diagnostics, history_admin, members, news, reports, sessions
from .app import AdminDesk, build_desk_from_env


@contextlib.contextmanager
def _open_db(path: str) -> Iterator[Any]:
    """A migrated connection that is always closed again (no leaked handles)."""

    conn = connect(path)
    try:
        apply_migrations(conn)
        yield conn
    finally:
        conn.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from wsgiref.simple_server import make_server

    desk_app = build_desk_from_env(db_path=args.db, release_dir=args.release_dir)
    host = str(args.host or os.environ.get("RANREPORT_ADMIN_HOST", "127.0.0.1"))
    port = int(str(args.port or os.environ.get("RANREPORT_ADMIN_PORT", "8098")))
    try:
        with make_server(host, port, desk_app) as httpd:
            print(f"editorial desk on http://{host}:{port}/admin/ (loopback is the default)", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:  # pragma: no cover - manual interrupt
                print("stopped", file=sys.stderr)
    finally:
        desk_app.close()
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    with _open_db(args.db) as conn:
        created = members.ensure_bootstrap(conn, args.telegram_id)
        active = len(members.active_members(conn))
    print(json.dumps({"bootstrapped": created, "members": active}, ensure_ascii=False))
    return 0


def cmd_member(args: argparse.Namespace) -> int:
    if bool(args.add) == bool(args.remove):
        print("give exactly one of --add / --remove", file=sys.stderr)
        return 2
    with _open_db(args.db) as conn:
        result = members.change_membership(conn, actor="cli", add=args.add, remove=args.remove)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    with _open_db(args.db) as conn:
        if args.revoke:
            count = sessions.revoke_all_for_user(conn, args.revoke, reason="cli")
            print(json.dumps({"revoked": count}, ensure_ascii=False))
            return 0
        rows = sessions.active_sessions(conn)
    print(json.dumps({"active_sessions": len(rows)}, ensure_ascii=False))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    with _open_db(args.db) as conn:
        written = history_admin.ingest(conn)
        conflicts = history_admin.detect_conflicts(conn)
        gaps = [gap["year"] for gap in history_admin.gaps(conn)]
    print(
        json.dumps(
            {
                "observations": written["written"],
                "conflicts": [conflict.conflict_id for conflict in conflicts],
                "gaps": gaps,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    with _open_db(args.db) as conn:
        findings = diagnostics.collect(conn, now=datetime.now(timezone.utc))
        counts = diagnostics.sync_issues(conn, findings, now=datetime.now(timezone.utc).isoformat())
    print(
        json.dumps(
            {"findings": [finding.code for finding in findings], "issue_counts": counts},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    with _open_db(args.db) as conn:
        result = reports.close_period(conn, label=args.period, actor=args.actor or "cli", reason=args.reason)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    with _open_db(args.db) as conn:
        if args.comment is not None:
            reports.save_draft(conn, label=args.period, actor=args.actor or "cli", comment=args.comment)
        built = reports.build(conn, label=args.period)
    print(built["markdown"])
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # every desk command checks, before it exits, that it left no SQLite handle,
    # listening socket or HTTP response open (see randreport/leakguard.py)
    leakguard.install(verbose=False)

    parser = argparse.ArgumentParser(prog="randreport.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the editorial desk over HTTP (loopback by default)")
    serve.add_argument("--db", default=None)
    serve.add_argument("--release-dir", default=None)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", default=None)
    serve.set_defaults(func=cmd_serve)

    bootstrap = sub.add_parser("bootstrap", help="seed the first member from the agreed Telegram id")
    bootstrap.add_argument("--db", required=True)
    bootstrap.add_argument("--telegram-id", required=True)
    bootstrap.set_defaults(func=cmd_bootstrap)

    member = sub.add_parser("member", help="add or revoke a member")
    member.add_argument("--db", required=True)
    member.add_argument("--add", default=None)
    member.add_argument("--remove", default=None)
    member.set_defaults(func=cmd_member)

    session = sub.add_parser("sessions", help="list or revoke sessions")
    session.add_argument("--db", required=True)
    session.add_argument("--revoke", default=None)
    session.set_defaults(func=cmd_sessions)

    history = sub.add_parser("history", help="ingest historical observations and conflicts")
    history.add_argument("--db", required=True)
    history.set_defaults(func=cmd_history)

    diagnose = sub.add_parser("diagnose", help="collect findings and sync issues")
    diagnose.add_argument("--db", required=True)
    diagnose.set_defaults(func=cmd_diagnose)

    close = sub.add_parser("close", help="close a period and freeze an immutable version")
    close.add_argument("--db", required=True)
    close.add_argument("--period", required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--actor", default=None)
    close.set_defaults(func=cmd_close)

    post = sub.add_parser("post", help="render the monthly draft post")
    post.add_argument("--db", required=True)
    post.add_argument("--period", required=True)
    post.add_argument("--comment", default=None)
    post.add_argument("--actor", default=None)
    post.set_defaults(func=cmd_post)

    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
