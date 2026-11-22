"""Command line entry point for the public site.

    python -m randreport.web serve   [--host 127.0.0.1 --port 8099]
    python -m randreport.web export  --out DIR
    python -m randreport.web seed    --db PATH
    python -m randreport.web correct --db PATH --period YYYY-MM --field expenses --value 123 --reason "..."
    python -m randreport.web build   --db PATH --release-dir DIR --periods YYYY-MM,YYYY
    python -m randreport.web check   [--release-dir DIR --db PATH]

Environment defaults: ``RANREPORT_RELEASE_DIR``, ``RANREPORT_DB``, ``RANREPORT_HOST``,
``RANREPORT_PORT``. No secret is read or printed here; the public process only
needs the read-only projection and the report store.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .. import leakguard
from ..bindguard import BindRefused, assert_bind_allowed
from ..domain.timeframes import PeriodError, parse_period
from ..store import apply_migrations, connect
from ..store import repository as repo
from . import reports as reports_module
from .app import PublicSite

PAGES = (
    "/",
    "/money",
    "/events",
    "/community",
    "/support",
    "/chronicle",
    "/reports",
    "/methodology",
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def _site(args: argparse.Namespace) -> PublicSite:
    return PublicSite(
        release_dir=getattr(args, "release_dir", None) or _env("RANREPORT_RELEASE_DIR"),
        db_path=getattr(args, "db", None) or _env("RANREPORT_DB"),
        static_dir=getattr(args, "static_dir", None) or _env("RANREPORT_STATIC_DIR"),
    )


@contextlib.contextmanager
def _open_db(path: str) -> Iterator[Any]:
    """A migrated connection that is always closed again (no leaked handles)."""

    conn = connect(path)
    try:
        apply_migrations(conn)
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _served_site(args: argparse.Namespace) -> Iterator[PublicSite]:
    """A site whose read-only report-store connection is released afterwards."""

    site = _site(args)
    try:
        yield site
    finally:
        site.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from wsgiref.simple_server import make_server

    host = str(args.host or _env("RANREPORT_HOST", "127.0.0.1"))
    port = int(str(args.port or _env("RANREPORT_PORT", "8099")))
    # the served process must not reach the private store: RANREPORT_DB is ignored
    # here on purpose and only an explicit staging flag can attach a report DB
    report_db = getattr(args, "report_db", None) or _env("RANREPORT_REPORT_DB")
    try:
        assert_bind_allowed(host, allow_external=bool(getattr(args, "allow_external_bind", False)))
    except BindRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    site = PublicSite(
        release_dir=getattr(args, "release_dir", None) or _env("RANREPORT_RELEASE_DIR"),
        db_path=report_db,
        static_dir=getattr(args, "static_dir", None) or _env("RANREPORT_STATIC_DIR"),
    )
    try:
        with make_server(host, port, site) as httpd:
            print(
                f"public site on http://{host}:{port}/  (release dir: {site.index.root}; "
                f"private db: {'staging only' if report_db else 'not opened'})",
                flush=True,
            )
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:  # pragma: no cover - manual interrupt
                print("stopped", file=sys.stderr)
    finally:
        site.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    periods = [p for p in (args.periods.split(",") if args.periods else []) if p]
    with _served_site(args) as site:
        written = site.export_static(args.out, periods=periods or None)
    print(json.dumps({"written": len(written), "out": str(Path(args.out).resolve())}, ensure_ascii=False))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    with _open_db(args.db) as conn:
        written = reports_module.seed_published_snapshots(conn)
    print(json.dumps({"seeded_versions": written}, ensure_ascii=False))
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    value = None if args.value is None else int(args.value)
    with _open_db(args.db) as conn:
        result = reports_module.record_correction(
            conn,
            period_label=args.period,
            changes={args.field: value},
            reason=args.reason,
            actor=args.actor,
        )
    print(json.dumps({"version": result["version"], "diff": result["diff"]}, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from .build import build_and_publish

    period_labels = [p for p in (args.periods.split(",") if args.periods else []) if p]
    if not period_labels:
        print("--periods is required (comma-separated YYYY-MM or YYYY)", file=sys.stderr)
        return 2
    # both paths are resolved (argument, then environment) and refused *before* anything
    # is opened: a build with no store would otherwise create a database named "None"
    # in the working directory, and one with no release directory would build into
    # nothing at all (card t_3eeb34dc)
    db = args.db or _env("RANREPORT_DB")
    if not db:
        print("a private store path is required (--db or RANREPORT_DB)", file=sys.stderr)
        return 2
    release_dir = args.release_dir or _env("RANREPORT_RELEASE_DIR")
    if not release_dir:
        print(
            "a release directory is required (--release-dir or RANREPORT_RELEASE_DIR)",
            file=sys.stderr,
        )
        return 2
    try:
        periods = [parse_period(label) for label in period_labels]
    except PeriodError as exc:
        # an unsupported period is a human refusal, not a traceback (the same
        # sentence the nightly CLI prints)
        print(f"unsupported period: {exc}", file=sys.stderr)
        return 2
    with _open_db(db) as conn:
        published = build_and_publish(
            conn,
            release_dir=release_dir,
            periods=periods,
            now=datetime.now(timezone.utc),
            freshness=args.freshness,
            verification=args.verification,
            source_labels=tuple(s for s in (args.source_labels or "").split(",") if s),
        )
    print(json.dumps({"published": published}, ensure_ascii=False))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    """Move the release pointer back to an existing, verified revision."""

    from ..ops.publish import PublishError, previous_published_release, rollback_release
    from .release_index import ReleaseIndex

    root = args.release_dir or _env("RANREPORT_RELEASE_DIR")
    if not root:
        print("a release directory is required (--release-dir or RANREPORT_RELEASE_DIR)", file=sys.stderr)
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
            release = ReleaseIndex(root).get(target)
            if release is None:
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


_LEAK_PATTERNS = (
    re.compile(r"\b(?:acc|occ|cat|sub|cal|pair|tx)_[A-Za-z0-9_]+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)

# the documented public schedule is the only clock value a page may state
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
ALLOWED_TIMES = frozenset({"03:00"})


def cmd_check(args: argparse.Namespace) -> int:
    """Scan every public page for forbidden ids, private fields and exact times."""

    problems: list[str] = []
    checked = 0
    with _served_site(args) as site:
        for period in site.available_periods():
            for route in PAGES:
                checked += 1
                query = f"period={period}"
                response = site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": query})
                text = response.text
                for pattern in _LEAK_PATTERNS:
                    for match in pattern.findall(text):
                        problems.append(f"{route}?{query}: {match!r}")
                for match in _TIME_RE.findall(text):
                    if match not in ALLOWED_TIMES:
                        problems.append(f"{route}?{query}: exact time {match!r}")
    print(json.dumps({"pages_checked": checked, "problems": problems[:50], "problem_count": len(problems)}, ensure_ascii=False))
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    # every maintenance command checks, before it exits, that it left no SQLite
    # handle, listening socket or HTTP response open (see randreport/leakguard.py)
    leakguard.install(verbose=False)

    parser = argparse.ArgumentParser(prog="randreport.web")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--release-dir", default=None)
        p.add_argument("--db", default=None)
        p.add_argument("--static-dir", default=None)

    serve = sub.add_parser("serve", help="run the public site over HTTP")
    common(serve)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", default=None)
    serve.add_argument(
        "--allow-external-bind",
        action="store_true",
        help="permit a non-loopback bind (also needs RANREPORT_ALLOW_EXTERNAL_BIND=1)",
    )
    serve.add_argument(
        "--report-db",
        default=None,
        help="staging only: attach a private report DB (RANREPORT_DB is never read by serve)",
    )
    serve.set_defaults(func=cmd_serve)

    export = sub.add_parser("export", help="write a static export of every page")
    common(export)
    export.add_argument("--out", required=True)
    export.add_argument("--periods", default=None, help="comma-separated period labels")
    export.set_defaults(func=cmd_export)

    seed = sub.add_parser("seed", help="seed the published snapshots of the report archive")
    seed.add_argument("--db", required=True)
    seed.set_defaults(func=cmd_seed)

    correct = sub.add_parser("correct", help="record a corrected version of a period report (R01)")
    correct.add_argument("--db", required=True)
    correct.add_argument("--period", required=True)
    correct.add_argument("--field", required=True, choices=["events", "receipts", "expenses"])
    correct.add_argument("--value", default=None)
    correct.add_argument("--reason", required=True)
    correct.add_argument("--actor", default=None)
    correct.set_defaults(func=cmd_correct)

    build = sub.add_parser("build", help="build and publish releases for periods from the private store")
    common(build)
    build.add_argument("--periods", required=True)
    build.add_argument("--freshness", default="fresh")
    build.add_argument("--verification", default="operational")
    build.add_argument("--source-labels", default=None)
    build.set_defaults(func=cmd_build)

    check = sub.add_parser("check", help="scan every public page for private leaks")
    common(check)
    check.set_defaults(func=cmd_check)

    rollback = sub.add_parser("rollback", help="point the release dir back at a verified revision")
    common(rollback)
    rollback.add_argument("--to", default=None, help="release id to roll back to")
    rollback.add_argument("--previous", action="store_true", help="use the release before the current one")
    rollback.add_argument("--dry-run", action="store_true", help="verify the target without moving the pointer")
    rollback.set_defaults(func=cmd_rollback)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except reports_module.ReportError as exc:
        # a refused correction or a missing snapshot is a human sentence, not a traceback
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
