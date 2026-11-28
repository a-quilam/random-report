"""Integration of the four release branches into one release candidate (t_60dd112b).

The merged tree is driven end to end on synthetic data only — no real credential,
source, file or paid service is touched:

1. **bootstrap** — a migrated private store carrying the confirmed public-contour
   map, the calendar mapping (meeting/rent/excluded contours) and one durable
   inbox delivery;
2. **sync** — the *real* nightly CLI pulls Zenmoney, Teamup and Telegram over
   loopback fixture APIs (the documented ``*_BASE_URL`` overrides);
3. **read** — the eight public sections render from the published release;
4. **CSV** — the public category export;
5. **correction** — a corrected report version, with its diff and reason;
6. **reconciliation** — ``web check`` over every page plus a month/year tie-out;
7. **archive** — the allowlisted report archive travels inside the release
   (``reports.json``), so the public process needs no private database;
8. **backup / clean restore** — an encrypted backup restored onto a clean path.

The meeting rules are asserted here as well: only the Moscow start day plus the
constant label, the excluded subcalendar contributes nothing, and nothing is
counted twice.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.inbox import store as inbox_store  # noqa: E402
from randreport.ops import backup as backup_module  # noqa: E402
from randreport.ops import cli as ops_cli  # noqa: E402
from randreport.projection.privacy import find_leaks  # noqa: E402
from randreport.store import apply_migrations, connect, schema_version  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from randreport.web import __main__ as web_main  # noqa: E402
from randreport.web import reports as web_reports  # noqa: E402
from randreport.web.app import PublicSite  # noqa: E402
from tests import fixtures as fx  # noqa: E402

PERIOD = "2025-07"
#: A period that already has a published report, so a correction is possible.
CORRECTED_PERIOD = "2025-03"
MEMBERS = 4128
CHANNEL = "@random_culture"
CALENDAR_KEY = "cal-synthetic"
TEAMUP_ID = "cal_community"
PASSPHRASE = "synthetic-passphrase-integration"

EIGHT_SECTIONS = ("/", "/money", "/events", "/community", "/support", "/chronicle", "/reports", "/methodology")
JSON_SECTIONS = ("/api/public/overview", "/api/public/money", "/api/public/events", "/api/public/community", "/api/public/chronicle")


# --------------------------------------------------------------------------- #
# synthetic source data and loopback APIs
# --------------------------------------------------------------------------- #


def _movement(tx_id: str, day: int, outcome: int, *, income: int = 0, tag: str, account: str = "acc_card") -> dict:
    """One transaction in the documented Zenmoney ``/v8/diff/`` shape.

    Both sides always carry the same account for an ordinary expense or income,
    which is exactly the shape whose misreading produced phantom transfers.
    """
    return {
        "id": tx_id,
        "date": f"2025-07-{day:02d}",
        "income": income / 100,
        "outcome": outcome / 100,
        "incomeAccount": account,
        "outcomeAccount": account,
        "incomeInstrument": 2,
        "outcomeInstrument": 2,
        "tag": [tag],
    }


MOVEMENTS = (
    _movement("tx_rent", 3, 400_000, tag="cat_rent"),
    _movement("tx_utilities", 5, 90_000, tag="cat_utilities"),
    _movement("tx_event_costs", 12, 40_000, tag="cat_event_costs"),
    _movement("tx_donations", 6, 0, income=120_000, tag="cat_donations"),
    _movement("tx_event_share", 18, 0, income=60_000, tag="cat_event_share"),
    _movement("tx_refund", 22, 0, income=5_000, tag="cat_purchase_refund"),
    _movement("tx_private", 28, 50_000, tag="cat_rent", account="acc_personal"),
)


def _event(occurrence_id: str, day: int, *, subcalendars, title: str | None) -> dict:
    event: dict = {
        "id": occurrence_id,
        "subcalendar_ids": list(subcalendars),
        "start_dt": f"2025-07-{day:02d}T19:00:00+03:00",
        "end_dt": f"2025-07-{day:02d}T21:00:00+03:00",
        "version": "6a1f2c9d4b",  # an opaque token, exactly as the real API sends it
    }
    if title is not None:
        event["title"] = title
    return event


CALENDAR = (
    _event("ev_open", 5, subcalendars=["sub_open"], title="Открытый концерт"),
    _event("ev_meeting", 19, subcalendars=["sub_meetings"], title="Собрания"),
    # merely *contains* the word: conservative exact-match means it is not a meeting
    _event("ev_meeting_long", 20, subcalendars=["sub_meetings"], title="Собрания по бюджету"),
    _event("ev_rent", 21, subcalendars=["sub_rent"], title="Аренда подвала"),
    _event("ev_closed", 22, subcalendars=["sub_closed"], title="Закрытая встреча коммунаров"),
    _event("ev_internal", 23, subcalendars=["sub_internal"], title="Собрание коммунаров"),
    _event("ev_prep", 24, subcalendars=["sub_preparation"], title="Репетиция"),
    # the excluded subcalendar (the "K5" record): neither lists nor totals
    _event("ev_excluded", 25, subcalendars=["sub_excluded"], title="K5 закрытое заседание"),
)


class _ApiHandler(BaseHTTPRequestHandler):
    """One loopback API for all three sources."""

    def log_message(self, *args):  # keep the test output quiet
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - the http.server API
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/v8/diff/"):
            self._json(self.server.zenmoney_payload())  # type: ignore[attr-defined]
        else:
            self._json({"error": "not found"}, status=404)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.endswith("/events"):
            self._json({"events": list(CALENDAR), "pages": {"next": None}})
        elif path.endswith("/getChatMemberCount"):
            self._json({"ok": True, "result": MEMBERS})
        else:
            self._json({"error": "not found"}, status=404)


class FixtureApi:
    """A loopback HTTP API answering Zenmoney, Teamup and Telegram."""

    def __init__(self, movements) -> None:
        self._movements = tuple(movements)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
        self.httpd.zenmoney_payload = self._payload  # type: ignore[attr-defined]
        self.thread = __import__("threading").Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _payload(self) -> dict:
        return {
            "serverTimestamp": 1000,
            "instrument": [{"id": 2, "title": "RUB", "shortTitle": "RUB", "symbol": "₽"}],
            "transaction": list(self._movements),
            "account": [
                {"id": "acc_card", "type": "ccard", "instrument": 2, "balance": 5000.0},
                {"id": "acc_cash", "type": "cash", "instrument": 2, "balance": 1200.0},
            ],
        }

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _mapping_payloads() -> tuple[dict, dict]:
    policy = fx.policy()
    mapping = fx.event_mapping()
    policy_payload = {
        "public_accounts": sorted(policy.public_accounts),
        "private_accounts": sorted(policy.private_accounts),
        "quarantined_accounts": sorted(policy.quarantined_accounts),
        "category_classes": {key: str(value) for key, value in policy.category_classes.items()},
        "category_labels": dict(fx.CATEGORY_LABELS),
        "quarantined_categories": sorted(policy.quarantined_categories),
        "commission_categories": sorted(policy.commission_categories),
        "subsidy_category_ids": sorted(policy.subsidy_category_ids),
        "count_visibility_threshold_kopecks": policy.count_visibility_threshold_kopecks,
        "sensitive_categories": [],
        "unclassified_label": policy.unclassified_label,
    }
    event_payload = {
        "subcalendar_types": {key: str(value) for key, value in mapping.subcalendar_types.items()},
        "private_subcalendars": sorted(mapping.private_subcalendars),
        "excluded_subcalendars": sorted(mapping.excluded_subcalendars),
        "meeting_subcalendars": sorted(mapping.meeting_subcalendars),
        "rent_subcalendars": sorted(mapping.rent_subcalendars),
        "meeting_titles": ["Собрания"],
        "meeting_label": "Собрание",
    }
    return policy_payload, event_payload


# --------------------------------------------------------------------------- #
# the end-to-end run
# --------------------------------------------------------------------------- #


class MergedReleaseEndToEnd(unittest.TestCase):
    """One bootstrap -> sync -> read -> correct -> archive -> backup chain."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="randreport-integration-"))
        cls.addClassCleanup(shutil.rmtree, cls.root, True)
        cls.db = cls.root / "randreport.sqlite"
        cls.releases = cls.root / "releases"
        cls.lease = cls.root / "nightly.lock"
        cls.backup = cls.root / "backup.sqlite.enc"
        cls.restored = cls.root / "restored.sqlite"
        cls.api = FixtureApi(MOVEMENTS)
        cls.addClassCleanup(cls.api.close)

        cls._bootstrap()
        cls.nightly_code, cls.nightly_report, cls.nightly_err = cls._run_cli(["run"])
        cls.release_id = cls.nightly_report.get("releases", {}).get(PERIOD, "")

    # --- helpers ---------------------------------------------------------- #

    @classmethod
    def _env(cls, **extra) -> dict:
        base = {
            "ZENMONEY_TOKEN": "synthetic-zenmoney-token",
            "ZENMONEY_BASE_URL": cls.api.base_url,
            "TEAMUP_CALENDAR_KEY": CALENDAR_KEY,
            "TEAMUP_CALENDAR_ID": TEAMUP_ID,
            "TEAMUP_BASE_URL": cls.api.base_url,
            "TELEGRAM_BOT_TOKEN": "synthetic-bot-token",
            "TELEGRAM_PUBLIC_CHANNEL": CHANNEL,
            "TELEGRAM_BASE_URL": cls.api.base_url,
            "RANREPORT_DB": str(cls.db),
            "RANREPORT_BACKUP_PASSPHRASE": PASSPHRASE,
            "NEWS_LLM_PROVIDER": "none",
        }
        base.update(extra)
        return base

    @classmethod
    def _bootstrap(cls) -> None:
        conn = connect(str(cls.db))
        try:
            apply_migrations(conn)
            policy_payload, event_payload = _mapping_payloads()
            repo.record_category_mapping(conn, version="policy-integration-1", payload=policy_payload)
            repo.record_event_mapping(conn, version="event-integration-1", payload=event_payload)
            # one durable delivery: the webhook already wrote it before the cycle
            inbox_store.accept_post(
                conn,
                inbox_store.InboxPost(
                    update_id="1001",
                    kind="channel_post",
                    channel_id=CHANNEL,
                    message_id="700",
                    posted_at=datetime(2025, 7, 12, 10, 0, tzinfo=fx.MSK),
                    text="Мы отремонтировали вход в подвал: новые двери и свет в коридоре.",
                ),
            )
            web_reports.seed_published_snapshots(conn)
        finally:
            conn.close()

    @classmethod
    def _run_cli(cls, argv: list[str], **env) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("TEAMUP_", "TELEGRAM_", "NEWS_", "ZENMONEY_", "RANREPORT_"))}
        clean.update(cls._env(**env))
        full = [
            argv[0],
            "--db",
            str(cls.db),
            "--publish-dir",
            str(cls.releases),
            "--lease",
            str(cls.lease),
            "--period",
            PERIOD,
            *argv[1:],
        ]
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ops_cli.main(full)
        text = out.getvalue().strip().splitlines()
        report = json.loads(text[-1]) if text else {}
        return code, report, err.getvalue()

    @classmethod
    def _section(cls, name: str) -> dict:
        path = cls.releases / cls.release_id / f"{name}.json"
        cls.assertTrue(path.exists(), f"release {cls.release_id} has no {name}.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def _public(self, path: str, query: str = f"period={PERIOD}"):
        site = PublicSite(release_dir=self.releases, db_path=None)  # no private store
        try:
            return site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query})
        finally:
            site.close()

    # --- 1. the nightly sync over loopback APIs ---------------------------- #

    def test_the_nightly_cli_syncs_every_source_and_publishes(self) -> None:
        self.assertEqual(self.nightly_code, 0, self.nightly_err)
        self.assertTrue(self.nightly_report["published"])
        self.assertTrue(self.release_id, self.nightly_report)
        sources = self.nightly_report["sources"]
        self.assertEqual(sources["zenmoney"]["status"], "ok")
        self.assertEqual(sources["teamup"]["status"], "ok")
        self.assertEqual(sources["telegram"]["status"], "ok")
        # the inbox can never prove completeness: partial with a reason, never "no news"
        self.assertEqual(sources["telegram_posts"]["coverage"], "partial")
        self.assertTrue(self.nightly_report["contour_confirmed"])

    def test_the_contour_map_is_the_recorded_one_not_a_hardcoded_default(self) -> None:
        money = self._section("money")
        buckets: dict[str, int] = {}
        for row in money["categories"]:
            buckets[row["bucket"]] = buckets.get(row["bucket"], 0) + row["amount_kopecks"]
        # the private account (acc_personal) never reaches the public contour
        self.assertEqual(buckets.get("operating_expense"), 400_000 + 90_000 + 40_000)
        self.assertEqual(buckets.get("external_compensation"), 120_000 + 60_000)
        self.assertGreater(money["metrics"]["expense_kopecks"], 0)

    def test_the_previous_defect_is_gone_money_is_not_published_as_zeros(self) -> None:
        # on the baseline the CLI built releases from a hardcoded empty policy and
        # the whole contour fell into quarantine; the merged CLI uses the record
        self.assertNotEqual(self._section("money")["metrics"].get("expense_kopecks"), 0)

    # --- 2. read the eight public sections without the private database ---- #

    def test_the_eight_sections_render_without_the_private_database(self) -> None:
        for path in EIGHT_SECTIONS:
            with self.subTest(path=path):
                response = self._public(path)
                self.assertEqual(response.status, 200, f"{path} -> {response.status}")

    def test_the_json_api_serves_every_analytical_section(self) -> None:
        for path in JSON_SECTIONS:
            with self.subTest(path=path):
                self.assertEqual(self._public(path).status, 200)

    # --- 3. the meeting rules (owner decision) ----------------------------- #

    def test_a_meeting_publishes_only_the_moscow_day_and_the_constant_label(self) -> None:
        events = self._section("events")
        self.assertEqual(events["meetings"], [{"on": "2025-07-19", "label": "Собрание"}])

    def test_the_public_payload_carries_no_other_text_or_id_for_a_meeting(self) -> None:
        raw = json.dumps(self._section("events"), ensure_ascii=False)
        for forbidden in ("Собрания", "Собрания по бюджету", "Аренда подвала", "Закрытая встреча", "ev_meeting", TEAMUP_ID):
            self.assertNotIn(forbidden, raw)

    def test_the_excluded_subcalendar_contributes_nothing(self) -> None:
        events = self._section("events")
        raw = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("K5", raw)
        self.assertNotIn("sub_excluded", raw)
        self.assertNotIn("ev_excluded", raw)
        # the excluded record is in neither the totals nor any aggregate
        self.assertEqual(events["metrics"]["total_events"], len(events["public_events"]) + sum(
            count for name, count in events["event_type_aggregates"].items() if name not in {e["type"] for e in events["public_events"]}
        ))

    def test_nothing_is_counted_twice(self) -> None:
        events = self._section("events")
        aggregates = events["event_type_aggregates"]
        self.assertEqual(events["metrics"]["total_events"], sum(aggregates.values()))
        self.assertEqual(
            events["metrics"]["closed_aggregate"],
            aggregates["closed"] + aggregates["closed_rent"],
        )
        self.assertEqual(events["metrics"]["meeting_aggregate"], aggregates["meeting"])
        self.assertEqual(events["metrics"]["meeting_aggregate"], 1)
        # exactly one meeting; the longer title stayed an aggregate-only closed record
        self.assertEqual(aggregates["closed"], 2)  # ev_closed + ev_meeting_long
        self.assertEqual(aggregates["closed_rent"], 1)  # ev_rent

    def test_no_private_identifier_leaks_into_the_release(self) -> None:
        problems = []
        for name in ("money", "events", "community", "chronicle"):
            problems.extend(find_leaks(self._section(name)))
        self.assertEqual(problems, [], f"leaks: {problems}")

    # --- 4. public CSV ----------------------------------------------------- #

    def test_the_public_csv_exports_categories_only(self) -> None:
        response = self._public("/api/public/export/categories.csv")
        self.assertEqual(response.status, 200)
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertTrue(rows, "the CSV has no category rows")
        text = response.text
        for forbidden in ("tx_", "acc_personal", "occurrence", "sub_"):
            self.assertNotIn(forbidden, text)

    # --- 5. correction -> archive ------------------------------------------ #

    def test_a_correction_becomes_a_visible_new_version_and_travels_in_the_archive(self) -> None:
        conn = connect(str(self.db))
        try:
            result = web_reports.record_correction(
                conn,
                period_label=CORRECTED_PERIOD,
                changes={"expenses": 123456},
                reason="сверка: уточнён расход месяца",
                actor="integrator-test",
            )
            self.assertGreaterEqual(result["version"], 2)
            self.assertEqual(result["diff"]["expenses"]["to"], 123456)
            view = web_reports.public_view(conn, CORRECTED_PERIOD)
            self.assertGreater(len(view["versions"]), 1)
            archive = web_reports.public_archive(conn)
        finally:
            conn.close()
        entry = archive[CORRECTED_PERIOD]
        reasons = json.dumps(entry, ensure_ascii=False)
        self.assertIn("сверка", reasons)
        # the correction author stays internal: only what/why is public
        self.assertNotIn("integrator-test", reasons)

    def test_archive_is_published_into_the_release_without_the_private_db(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("TEAMUP_", "TELEGRAM_", "NEWS_", "ZENMONEY_", "RANREPORT_"))}
        clean.update(self._env())
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = web_main.main(["build", "--db", str(self.db), "--release-dir", str(self.releases), "--periods", CORRECTED_PERIOD])
        self.assertEqual(code, 0, err.getvalue())
        published = json.loads(out.getvalue().strip().splitlines()[-1])["published"]
        release_dir = self.releases / published[-1][1]
        archive_path = release_dir / "reports.json"
        self.assertTrue(archive_path.exists(), "the release carries no report archive")
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertIn(CORRECTED_PERIOD, archive)
        # the correction author is internal: the published archive names no actor
        self.assertNotIn("actor", json.dumps(archive, ensure_ascii=False))
        self.assertNotIn("integrator-test", json.dumps(archive, ensure_ascii=False))
        # and the public process renders /reports from the release alone
        site = PublicSite(release_dir=self.releases, db_path=None)
        try:
            self.assertEqual(site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": f"/reports/{CORRECTED_PERIOD}", "QUERY_STRING": ""}).status, 200)
        finally:
            site.close()

    # --- 6. reconciliation ------------------------------------------------- #

    def test_reconciliation_month_against_the_year_ties_out(self) -> None:
        money = self._section("money")
        expenses = sum(row["amount_kopecks"] for row in money["categories"] if row["bucket"] == "operating_expense")
        self.assertEqual(expenses, 530_000)

    def test_web_check_finds_no_problem_on_any_page(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("TEAMUP_", "TELEGRAM_", "NEWS_", "ZENMONEY_", "RANREPORT_"))}
        clean.update(self._env())
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = web_main.main(["check", "--release-dir", str(self.releases)])
        report = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(report["problems"], [])
        self.assertEqual(code, 0, err.getvalue())

    # --- 7. backup and a clean restore ------------------------------------- #

    def test_encrypted_backup_restores_onto_a_clean_instance(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("TEAMUP_", "TELEGRAM_", "NEWS_", "ZENMONEY_", "RANREPORT_"))}
        clean.update(self._env())
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ops_cli.main(["backup", "--db", str(self.db), "--out", str(self.backup), "--encrypt"])
        self.assertEqual(code, 0, err.getvalue())
        manifest = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertTrue(manifest["encrypted"])
        self.assertTrue(self.backup.exists())

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ops_cli.main(["restore", "--backup", str(self.backup), "--db", str(self.restored)])
        self.assertEqual(code, 0, err.getvalue())
        report = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertTrue(report["usable"])
        self.assertEqual(report["missing_tables"], [])

        source = connect(str(self.db))
        restored = connect(str(self.restored))
        try:
            self.assertEqual(schema_version(restored), schema_version(source))
            self.assertEqual(
                restored.execute("SELECT COUNT(*) AS n FROM calendar_occurrences").fetchone()["n"],
                source.execute("SELECT COUNT(*) AS n FROM calendar_occurrences").fetchone()["n"],
            )
            self.assertEqual(
                restored.execute("SELECT COUNT(*) AS n FROM report_snapshots").fetchone()["n"],
                source.execute("SELECT COUNT(*) AS n FROM report_snapshots").fetchone()["n"],
            )
        finally:
            source.close()
            restored.close()

    def test_a_restore_without_the_passphrase_is_refused(self) -> None:
        target = self.root / "wrong.sqlite"
        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("TEAMUP_", "TELEGRAM_", "NEWS_", "ZENMONEY_", "RANREPORT_"))}
        clean.update(self._env())
        clean.pop(backup_module.PASSPHRASE_ENV, None)
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ops_cli.main(["restore", "--backup", str(self.backup), "--db", str(target)])
        self.assertNotEqual(code, 0)
        self.assertFalse(target.exists())

    # --- 8. the public process never needs the private store --------------- #

    def test_the_served_site_is_constructed_without_the_private_db(self) -> None:
        # `web serve` deliberately ignores RANREPORT_DB; nothing in this site
        # touched the private store, and every section above still rendered
        site = PublicSite(release_dir=self.releases, db_path=None)
        try:
            self.assertIsNone(site.db_path if hasattr(site, "db_path") else None)
            for path in EIGHT_SECTIONS:
                self.assertEqual(site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": f"period={PERIOD}"}).status, 200)
        finally:
            site.close()


class CalendarMappingPrecedence(unittest.TestCase):
    """The nightly CLI must apply the private calendar rules it is given.

    ``docs/CALENDAR.md`` section 4 fixes the precedence: the versioned row the
    editorial desk recorded in the store, then a private mapping file
    (``--events`` / ``RANREPORT_EVENT_MAPPING``), then the stable-ID environment
    configuration.  Before this, the nightly read the store *only*, so the
    documented ``TEAMUP_*`` surface was silently ignored and the owner's rules
    could not be applied to a staging cycle at all.

    The owner's recorded rule (``закрытый файл правил владельца``: ``source_title_contains_word``
    with ``case_sensitive: false``) is exercised here through the CLI as well: the
    longer title ``Собрания по бюджету`` is a meeting under ``contains`` and stays
    aggregate-only under the conservative ``exact`` default.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="randreport-mapping-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = self.root / "randreport.sqlite"
        self.releases = self.root / "releases"
        self.lease = self.root / "nightly.lock"
        self.api = FixtureApi(())
        self.addCleanup(self.api.close)
        conn = connect(str(self.db))
        try:
            apply_migrations(conn)
        finally:
            conn.close()

    def _run(self, **env) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("TEAMUP_", "TELEGRAM_", "NEWS_", "ZENMONEY_", "RANREPORT_"))}
        clean.update(
            {
                "TEAMUP_CALENDAR_KEY": CALENDAR_KEY,
                "TEAMUP_CALENDAR_ID": TEAMUP_ID,
                "TEAMUP_BASE_URL": self.api.base_url,
                "RANREPORT_DB": str(self.db),
            }
        )
        clean.update(env)
        argv = [
            "run",
            "--db",
            str(self.db),
            "--publish-dir",
            str(self.releases),
            "--lease",
            str(self.lease),
            "--period",
            PERIOD,
        ]
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ops_cli.main(argv)
        text = out.getvalue().strip().splitlines()
        report = json.loads(text[-1]) if text else {}
        return code, report, err.getvalue()

    def _events(self, release_id: str) -> dict:
        path = self.releases / release_id / "events.json"
        self.assertTrue(path.exists(), f"no published release {release_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _owner_env(**extra) -> dict:
        """The owner's recorded calendar rules as the documented TEAMUP_* surface."""

        base = {
            "TEAMUP_SUBCALENDAR_TYPES": (
                "sub_closed=closed,sub_open=open,sub_preparation=preparation,"
                "sub_internal=internal_meeting,sub_rent=closed,sub_meetings=closed"
            ),
            "TEAMUP_PRIVATE_SUBCALENDARS": "sub_closed,sub_internal",
            "TEAMUP_EXCLUDED_SUBCALENDARS": "sub_excluded",
            "TEAMUP_MEETING_SUBCALENDARS": "sub_meetings",
            "TEAMUP_RENT_SUBCALENDARS": "sub_rent",
            "TEAMUP_MEETING_TITLES": "собрание,собрания",
            "TEAMUP_MEETING_LABEL": "Собрание",
            "TEAMUP_MEETING_MATCH": "contains",
        }
        base.update(extra)
        return base

    def test_the_environment_map_is_applied_when_the_store_has_no_version(self) -> None:
        code, report, err = self._run(**self._owner_env())
        self.assertEqual(0, code, err)
        events = self._events(report["release_id"])
        days = {row["on"] for row in events["meetings"]}
        # both the exact title and the longer title that merely contains the word
        self.assertEqual({"2025-07-19", "2025-07-20"}, days)
        self.assertEqual(2, events["metrics"]["meeting_aggregate"])
        for row in events["meetings"]:
            self.assertEqual({"on", "label"}, set(row))
            self.assertEqual("Собрание", row["label"])
        # the excluded record contributes nothing and the raw titles never travel
        self.assertEqual(0, events["event_type_aggregates"].get("excluded", 0))
        self.assertNotIn("Собрания по бюджету", json.dumps(events, ensure_ascii=False))

    def test_the_conservative_exact_default_publishes_only_the_exact_title(self) -> None:
        code, report, err = self._run(**self._owner_env(TEAMUP_MEETING_TITLES="Собрания", TEAMUP_MEETING_MATCH="exact"))
        self.assertEqual(0, code, err)
        events = self._events(report["release_id"])
        self.assertEqual(["2025-07-19"], [row["on"] for row in events["meetings"]])

    def test_a_recorded_desk_version_wins_over_the_environment(self) -> None:
        _, event_payload = _mapping_payloads()
        conn = connect(str(self.db))
        try:
            repo.record_event_mapping(conn, version="desk-version-1", payload=event_payload)
        finally:
            conn.close()
        code, report, err = self._run(**self._owner_env())
        self.assertEqual(0, code, err)
        events = self._events(report["release_id"])
        # the desk's exact title («Собрания») wins: the longer title is not promoted
        self.assertEqual(["2025-07-19"], [row["on"] for row in events["meetings"]])

    def test_a_private_mapping_file_is_used_between_the_store_and_the_environment(self) -> None:
        _, event_payload = _mapping_payloads()
        path = self.root / "calendar_mapping.json"
        payload = dict(event_payload, meeting_match="contains", meeting_titles=["собрание", "собрания"])
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        code, report, err = self._run(**self._owner_env(TEAMUP_MEETING_MATCH="exact"), RANREPORT_EVENT_MAPPING=str(path))
        self.assertEqual(0, code, err)
        events = self._events(report["release_id"])
        self.assertEqual(["2025-07-19", "2025-07-20"], [row["on"] for row in events["meetings"]])

    def test_an_unusable_mapping_is_refused_before_anything_is_published(self) -> None:
        code, _, err = self._run(RANREPORT_EVENT_MAPPING=str(self.root / "absent.json"))
        self.assertEqual(5, code)
        self.assertIn("cannot load the event mapping", err)
        self.assertFalse(self.releases.exists() and any(self.releases.iterdir()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
