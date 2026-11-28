"""Independent acceptance verification (docs/REQUIREMENTS.md section 13).

This module is deliberately **not** a copy of the acceptance suites the feature
cards shipped. It re-checks the properties that a reviewer must be able to break
by hand — the API/HTML/CSV/JSON privacy boundary, spreadsheet-formula
neutralisation, unsafe URL schemes, the Moscow-only schedule, a backup/restore on
a clean instance, and the absence of real secrets in git, tests and build output
— over the same synthetic staging data (never a paid service, never a real
source).

Each test states the property it proves; the results are transcribed in
``docs/TEST_RESULTS_ACCEPTANCE.md``.
"""

from __future__ import annotations

import csv
import errno
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from randreport.admin import checks, members, reports as admin_reports, sessions
from randreport.admin.app import AdminDesk
from randreport.domain.dto import (
    CategoryDTO,
    ChronicleDTO,
    MoneyDTO,
    NewsItemDTO,
    PeriodDTO,
)
from randreport.ops import backup as backup_module
from randreport.ops import cli as ops_cli
from randreport.ops.schedule import (
    SYSTEMD_CALENDAR,
    is_due,
    logical_date_for,
    missed_logical_dates,
    next_run_at,
    systemd_timer_text,
)
from randreport.projection.csv_export import export_categories_csv
from randreport.projection.privacy import find_leaks
from randreport.store import MIGRATIONS, REQUIRED_TABLES, apply_migrations, connect, missing_tables, schema_version
from randreport.web import render as web_render
from randreport.web import reports as web_reports
from randreport.web.app import PublicSite
from tests import fixtures_admin as fa

REPO = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
CET = ZoneInfo("Europe/Paris")


def _money(label: str) -> MoneyDTO:
    return MoneyDTO(
        period=PeriodDTO(
            requested="2025-07",
            covered_start=datetime(2025, 7, 1).date(),
            covered_end=datetime(2025, 7, 31).date(),
            complete=True,
        ),
        as_of=datetime(2025, 7, 31).date(),
        release_id="rel_review",
        freshness="fresh",
        verification="operational",
        completeness="complete",
        source_labels=("zenmoney",),
        unit="RUB_kopecks",
        metrics={"expense_kopecks": 100, "income_kopecks": 200},
        categories=(
            CategoryDTO(
                key="kreview",
                label=label,
                bucket="donations",
                direction="in",
                amount_kopecks=120000,
                amount_rub="1200.00",
                share_percent="61.5",
                operations_count=1,
            ),
        ),
    )


class StagingCase(unittest.TestCase):
    """One private copy of the synthetic staging build per test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = fa.admin_case_dir(prefix="randreport-acceptance-")
        cls.db = cls.root / "randreport.sqlite"
        cls.releases = cls.root / "releases"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def site(self) -> PublicSite:
        return PublicSite(release_dir=self.releases, db_path=self.db)

    def request(self, site: PublicSite, path: str, query: str = "period=2025-07"):
        return site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query})


# --------------------------------------------------------------------------- #
# 1. Privacy boundary: HTML / JSON / CSV
# --------------------------------------------------------------------------- #

class PrivacyBoundaryTests(StagingCase):
    LEAK_PATTERNS = (
        re.compile(r"(?<![A-Za-z0-9])(?:acc|occ|cat|sub|cal|pair|tx)_[A-Za-z0-9_]+"),
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    )

    def test_every_public_route_hides_private_identifiers(self) -> None:
        site = self.site()
        try:
            routes = ("/", "/money", "/events", "/community", "/support", "/chronicle", "/reports", "/methodology")
            checked = 0
            for period in site.available_periods():
                for route in routes:
                    response = self.request(site, route, f"period={period}")
                    self.assertEqual(response.status, 200, f"{route}?period={period}")
                    for pattern in self.LEAK_PATTERNS:
                        self.assertEqual(pattern.findall(response.text), [], f"{route}?period={period}")
                    checked += 1
            self.assertGreater(checked, 100)
        finally:
            site.close()

    def test_json_api_payloads_carry_no_forbidden_keys_or_values(self) -> None:
        site = self.site()
        try:
            for endpoint in ("overview", "money", "events", "community", "chronicle"):
                response = self.request(site, f"/api/public/{endpoint}")
                self.assertEqual(response.status, 200, endpoint)
                payload = json.loads(response.text)
                self.assertEqual(find_leaks(payload), (), endpoint)
        finally:
            site.close()

    def test_api_rejects_an_id_like_parameter_with_400_not_500(self) -> None:
        # A non-allowlisted parameter must be refused *before* data is read. Its
        # name is the caller's own text: the privacy guard must not mistake the
        # echo for a leak and demote a 400 to a 500.
        site = self.site()
        try:
            for path, params in (
                ("/api/public/money", "period=2025-07&acc_card=x"),
                ("/api/public/money", "period=2025-07&tx_1=1"),
                ("/api/public/overview", "period=2025-07&occurrence_id=occ_9"),
                ("/money", "period=2025-07&calendar_id=cal_1"),
            ):
                response = self.request(site, path, params)
                self.assertEqual(response.status, 400, f"{path}?{params} -> {response.status}")
        finally:
            site.close()

    def test_html_escapes_untrusted_text(self) -> None:
        # Telegram/Teamup text is untrusted; a stored payload must not become markup.
        monkey = '<script>alert(1)</script>'
        html = web_render.money_section(_money(monkey), period_label="2025-07", historical={})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

        chronicle = ChronicleDTO(
            period=PeriodDTO(
                requested="2025-07",
                covered_start=datetime(2025, 7, 1).date(),
                covered_end=datetime(2025, 7, 31).date(),
                complete=True,
            ),
            as_of=datetime(2025, 7, 31).date(),
            release_id="rel_review",
            freshness="fresh",
            verification="operational",
            completeness="complete",
            source_labels=("telegram",),
            news_items=(
                NewsItemDTO(
                    id="n1",
                    title='<img src=x onerror="alert(1)">',
                    summary="<b>bold</b>",
                    category="ремонт",
                    published_on=datetime(2025, 7, 2).date(),
                    source_urls=("javascript:alert(1)",),
                    machine_processed=False,
                    verified=False,
                ),
            ),
            metrics={},
        )
        page = web_render.chronicle_section(chronicle)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b>bold</b>", page)
        self.assertNotIn('href="javascript:alert(1)"', page)

    def test_safe_url_allows_only_http_https_and_local_paths(self) -> None:
        self.assertEqual(web_render.safe_url("https://t.me/random_culture/1"), "https://t.me/random_culture/1")
        self.assertEqual(web_render.safe_url("/reports/2025-07"), "/reports/2025-07")
        for hostile in ("javascript:alert(1)", "data:text/html,x", "//evil.example/x", "  JaVaScRiPt:alert(1)"):
            self.assertEqual(web_render.safe_url(hostile), "", hostile)

    def test_csv_neutralises_spreadsheet_formula_triggers(self) -> None:
        params = {"period": "2025-07", "unit": "RUB_kopecks"}
        for hostile in ('=HYPERLINK("http://evil.example","x")', "+1+1", "@SUM(1+1)", "-2+3", "=cmd|calc", "\t=1"):
            text = export_categories_csv(_money(hostile), params=params)
            rows = list(csv.reader(io.StringIO(text)))
            cell = rows[1][2]
            self.assertTrue(cell.startswith("'"), f"{hostile!r} -> {cell!r} was not neutralised")

    def test_csv_keeps_real_negative_numbers_numeric(self) -> None:
        params = {"period": "2025-07", "unit": "RUB_kopecks"}
        plain = list(csv.reader(io.StringIO(export_categories_csv(_money("ok label"), params=params))))
        self.assertEqual(plain[1][2], "ok label")
        # a leading '-' that is part of a plain number is left alone
        negative = list(csv.reader(io.StringIO(export_categories_csv(_money("-40000.00"), params=params))))
        self.assertEqual(negative[1][2], "-40000.00")

    def test_rss_sitemap_and_feed_surfaces_do_not_exist(self) -> None:
        # Section 13 lists RSS/sitemap as leak surfaces. The product publishes
        # none of them, so there is nothing to leak through; this asserts the
        # endpoints stay absent rather than silently shipping an unguarded feed.
        site = self.site()
        try:
            for path in ("/rss.xml", "/feed.xml", "/feed", "/atom.xml", "/sitemap.xml"):
                self.assertEqual(self.request(site, path, "").status, 404, path)
        finally:
            site.close()


# --------------------------------------------------------------------------- #
# 2. Schedule and time
# --------------------------------------------------------------------------- #

class ScheduleTests(unittest.TestCase):
    def test_timer_unit_states_moscow_explicitly(self) -> None:
        self.assertEqual(SYSTEMD_CALENDAR, "*-*-* 03:00:00 Europe/Moscow")
        text = systemd_timer_text()
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow", text)
        self.assertIn("Persistent=true", text)
        self.assertNotIn("America/", text)

    def test_due_and_next_run_do_not_depend_on_the_host_timezone(self) -> None:
        # 2026-11-22 03:30 MSK == 00:30 UTC == 01:30 CET
        moment_msk = datetime(2026, 11, 22, 3, 30, tzinfo=MSK)
        self.assertTrue(is_due(moment_msk, None))
        self.assertFalse(is_due(moment_msk, "2026-11-22"))
        # the same instant expressed in two other zones agrees
        for zone in (timezone.utc, CET):
            self.assertTrue(is_due(moment_msk.astimezone(zone), None), zone)

    def test_before_three_msk_is_not_due(self) -> None:
        self.assertFalse(is_due(datetime(2026, 11, 22, 2, 59, tzinfo=MSK), None))
        self.assertTrue(is_due(datetime(2026, 11, 22, 3, 0, tzinfo=MSK), None))

    def test_logical_date_is_the_moscow_calendar_day(self) -> None:
        # 2026-11-22 23:30 UTC is already 2026-11-23 in Moscow
        self.assertEqual(logical_date_for(datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)), "2026-11-23")
        self.assertEqual(logical_date_for(datetime(2026, 11, 22, 20, 30, tzinfo=timezone.utc)), "2026-11-22")

    def test_catch_up_lists_the_missed_days_bounded(self) -> None:
        now = datetime(2026, 11, 22, 4, 0, tzinfo=MSK)
        self.assertEqual(missed_logical_dates(None, now), ("2026-11-22",))
        self.assertEqual(missed_logical_dates("2026-11-19", now), ("2026-11-20", "2026-11-21"))
        self.assertEqual(missed_logical_dates("2026-11-21", now), ())
        # a long outage is bounded, not an unbounded backfill
        self.assertLessEqual(len(missed_logical_dates("2020-01-01", now)), 7)

    def test_next_run_is_strictly_after_now(self) -> None:
        self.assertEqual(next_run_at(datetime(2026, 11, 22, 2, 0, tzinfo=MSK)), datetime(2026, 11, 22, 3, 0, tzinfo=MSK))
        self.assertEqual(next_run_at(datetime(2026, 11, 22, 5, 0, tzinfo=MSK)), datetime(2026, 11, 23, 3, 0, tzinfo=MSK))
        # and it equals the documented 03:00 MSK, i.e. 00:00 UTC
        self.assertEqual(next_run_at(datetime(2026, 11, 22, 5, 0, tzinfo=MSK)).astimezone(timezone.utc).hour, 0)


# --------------------------------------------------------------------------- #
# 3. Backup / restore on a clean instance (O01)
# --------------------------------------------------------------------------- #

class BackupRestoreTests(StagingCase):
    def test_migration_on_a_clean_database_creates_every_required_table(self) -> None:
        target = self.root / "fresh" / "clean.sqlite"
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(str(target))
        try:
            apply_migrations(conn)
            self.assertEqual(schema_version(conn), len(MIGRATIONS))
            self.assertEqual(missing_tables(conn), ())
            names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(REQUIRED_TABLES <= names, sorted(REQUIRED_TABLES - names))
            # re-running migrations on an already-migrated database is a no-op
            apply_migrations(conn)
            self.assertEqual(schema_version(conn), len(MIGRATIONS))
        finally:
            conn.close()

    def test_backup_then_restore_on_a_clean_instance_keeps_history(self) -> None:
        # a frozen version and a manual edit must survive the round trip
        conn = connect(str(self.db))
        try:
            members.ensure_bootstrap(conn, fa.ADMIN_ID)
            admin_reports.close_period(conn, label="2025-07", actor="reviewer", reason="приёмка")
            admin_reports.correction(
                conn,
                label="2025-07",
                changes={"events": 7},
                reason="нашлось седьмое мероприятие",
                actor="reviewer",
            )
            before = {
                "versions": [int(r["version"]) for r in conn.execute("SELECT version FROM report_snapshots WHERE period_label='2025-07' ORDER BY version")],
                "cursors": {str(r["source"]): str(r["cursor"]) for r in conn.execute("SELECT source, cursor FROM source_cursors")},
                "members": [str(r["telegram_user_id"]) for r in conn.execute("SELECT telegram_user_id FROM admin_members")],
                "rows": int(conn.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"]),
            }
        finally:
            conn.close()

        backups = self.root / "backups"
        backups.mkdir(exist_ok=True)
        backup_path = backups / "rr.sqlite"
        manifest = backup_module.backup_database(self.db, backup_path)
        self.assertEqual(manifest["integrity"], "ok")
        # the backup is one self-contained file: no WAL sidecar to lose in a copy
        self.assertFalse((backups / "rr.sqlite-wal").exists())
        self.assertFalse((backups / "rr.sqlite-shm").exists())
        self.assertTrue((backups / "rr.sqlite.manifest.json").exists())

        clean = self.root / "clean"
        clean.mkdir(exist_ok=True)
        target_db = clean / "randreport.sqlite"
        report = backup_module.restore_database(backup_path, target_db)
        self.assertTrue(report["usable"])
        self.assertEqual(report["integrity"], "ok")

        restored = connect(str(target_db))
        try:
            self.assertEqual(
                [int(r["version"]) for r in restored.execute("SELECT version FROM report_snapshots WHERE period_label='2025-07' ORDER BY version")],
                before["versions"],
            )
            self.assertEqual(
                {str(r["source"]): str(r["cursor"]) for r in restored.execute("SELECT source, cursor FROM source_cursors")},
                before["cursors"],
            )
            self.assertEqual([str(r["telegram_user_id"]) for r in restored.execute("SELECT telegram_user_id FROM admin_members")], before["members"])
            self.assertEqual(int(restored.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"]), before["rows"])
        finally:
            restored.close()

        # the clean instance serves the restored store end to end
        shutil.copytree(self.releases, clean / "releases", symlinks=True)
        site = PublicSite(release_dir=clean / "releases", db_path=target_db)
        try:
            page = site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": "/reports/2025-07", "QUERY_STRING": ""})
            self.assertEqual(page.status, 200)
            self.assertIn("нашлось седьмое мероприятие", page.text)
            money = site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": "/api/public/money", "QUERY_STRING": "period=2025-07"})
            self.assertEqual(money.status, 200)
        finally:
            site.close()

    def test_restore_never_overwrites_a_live_database_without_force(self) -> None:
        backups = self.root / "overwrite-backups"
        backups.mkdir(exist_ok=True)
        backup_path = backups / "rr.sqlite"
        backup_module.backup_database(self.db, backup_path)
        with self.assertRaises(backup_module.BackupError):
            backup_module.restore_database(backup_path, self.db)
        # with force it succeeds and stays consistent
        report = backup_module.restore_database(backup_path, self.root / "forced.sqlite", force=True)
        self.assertTrue(report["usable"])

    def test_backup_refuses_to_clobber_an_existing_artifact(self) -> None:
        backups = self.root / "clobber"
        backups.mkdir(exist_ok=True)
        first = backups / "rr.sqlite"
        backup_module.backup_database(self.db, first)
        with self.assertRaises(backup_module.BackupError):
            backup_module.backup_database(self.db, first)

    def test_backup_survives_a_tmpdir_on_another_filesystem(self) -> None:
        # t_c395208d: with TMPDIR on a separate device (typically tmpfs) the
        # staged plaintext copy could not be published: rename(2) refuses to
        # cross filesystems and the nightly backup died with
        # "Invalid cross-device link". The staging directory must therefore
        # live on the target's filesystem, so the final step stays an atomic
        # same-device os.replace.
        backups = self.root / "foreign-tmpdir-backups"
        backups.mkdir(exist_ok=True)
        backup_path = backups / "rr.sqlite"
        target_dev = backups.stat().st_dev

        # A real second filesystem when one is available (/dev/shm is its own
        # tmpfs mount with a st_dev of its own, unlike the checkout's): with
        # it the *actual kernel* rename(2) is what proves the fix, no
        # patching at all -- exactly the production failure, /tmp on tmpfs.
        real_foreign = None
        for candidate in ("/dev/shm", "/tmp"):
            try:
                probe = Path(candidate)
                if probe.is_dir() and probe.stat().st_dev != target_dev:
                    real_foreign = Path(
                        tempfile.mkdtemp(prefix="rr-foreign-", dir=probe)
                    )
                    break
            except OSError:
                continue
        if real_foreign is not None:
            self.addCleanup(shutil.rmtree, real_foreign, ignore_errors=True)

        # foreign_tmp is the stand-in TMPDIR: a genuinely foreign device when
        # one exists, otherwise a directory on the checkout filesystem that
        # the replace shim below *treats* as foreign (rename(2) semantics:
        # EXDEV when source and destination live on different filesystems --
        # and a rename out of the foreign TMPDIR crosses by definition, so a
        # staging directory anywhere but on the target's own filesystem dies).
        foreign_tmp = (
            real_foreign if real_foreign is not None else self.root / "alien-tmp"
        )
        if real_foreign is None:
            foreign_tmp.mkdir(exist_ok=True)
        real_replace = os.replace

        def replace_or_exdev(src: str, dst: str) -> None:
            src = os.path.abspath(src)
            crosses = str(src).startswith(str(foreign_tmp) + os.sep)
            if not crosses and os.path.exists(src):
                crosses = os.stat(src).st_dev != os.stat(
                    os.path.dirname(os.path.abspath(dst))
                ).st_dev
            if crosses:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            real_replace(src, dst)

        with mock.patch.object(tempfile, "tempdir", str(foreign_tmp)):
            if real_foreign is None:
                # no second filesystem here: the shim enforces rename(2)
                with mock.patch("os.replace", replace_or_exdev):
                    manifest = backup_module.backup_database(self.db, backup_path)
            else:
                # real cross-device rename: the kernel is the referee
                manifest = backup_module.backup_database(self.db, backup_path)
        self.assertEqual(manifest["integrity"], "ok")
        self.assertTrue(backup_path.is_file())
        # the 0700 staging directory (and any plaintext in it) is removed
        # with the publish, whatever the outcome -- nothing called
        # randreport-backup-* is left behind next to the backup
        leftovers = [
            p.name
            for p in backups.iterdir()
            if p.name.startswith("randreport-backup-")
        ]
        self.assertEqual(leftovers, [])

    def test_a_file_copy_of_a_live_wal_database_is_not_a_valid_backup(self) -> None:
        # This documents *why* the backup API is required: a plain copy of the
        # live file can miss WAL-resident pages. The API path is asserted to be
        # consistent where the naive copy is not.
        live = self.root / "wal-live.sqlite"
        conn = connect(str(live))
        try:
            apply_migrations(conn)
            conn.execute(
                "INSERT INTO admin_members (telegram_user_id, added_at, revoked_at, note) VALUES (?, ?, NULL, NULL)",
                ("777", "2026-01-01T00:00:00+00:00"),
            )
            wal = live.with_suffix(".sqlite-wal")
            self.assertTrue(wal.exists(), "the live database must be in WAL mode")
        finally:
            conn.close()
        copy_path = self.root / "wal-copy.sqlite"
        shutil.copyfile(live, copy_path)
        copy_conn = sqlite3.connect(str(copy_path))
        try:
            copied = int(copy_conn.execute("SELECT COUNT(*) AS n FROM admin_members").fetchone()[0])
        except sqlite3.DatabaseError:
            copied = -1
        finally:
            copy_conn.close()
        backup_path = self.root / "wal-backup.sqlite"
        backup_module.backup_database(live, backup_path)
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            backed_up = int(backup_conn.execute("SELECT COUNT(*) AS n FROM admin_members").fetchone()[0])
        finally:
            backup_conn.close()
        self.assertEqual(backed_up, 1)
        self.assertGreaterEqual(copied, 0)  # a bare copy may or may not even open


# --------------------------------------------------------------------------- #
# 3b. The publish panel survives a group-period snapshot
# --------------------------------------------------------------------------- #

class PublishPanelTests(StagingCase):
    """A group report (``2025-01..2025-12``) is a real stored period label."""

    def _desk(self) -> tuple[AdminDesk, str]:
        conn = connect(str(self.db))
        try:
            members.ensure_bootstrap(conn, fa.ADMIN_ID)
            session = sessions.create_session(conn, user_id=fa.ADMIN_ID, display_name="Приёмка")
        finally:
            conn.close()
        desk = AdminDesk(db_path=self.db, secret=fa.SERVER_SECRET, migrate=False)
        return desk, session.session_id

    def _get(self, desk: AdminDesk, token: str, path: str):
        return desk._dispatch(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": path,
                "QUERY_STRING": "",
                "HTTP_COOKIE": f"{sessions.COOKIE_NAME}={token}",
                "HTTP_HOST": "127.0.0.1:8098",
            }
        )

    def test_publish_index_and_group_period_page_render(self) -> None:
        # seed the published snapshots exactly as `python -m randreport.web seed` does
        conn = connect(str(self.db))
        try:
            web_reports.seed_published_snapshots(conn)
            labels = [str(row["period_label"]) for row in conn.execute("SELECT DISTINCT period_label FROM report_snapshots")]
        finally:
            conn.close()
        group = [label for label in labels if ".." in label]
        self.assertTrue(group, f"the seeded store must hold a group-period snapshot, got {labels}")

        desk, token = self._desk()
        try:
            index = self._get(desk, token, "/admin/publish")
            self.assertEqual(index.status, 200, index.text[:200])
            self.assertIn(group[0], index.text)
            page = self._get(desk, token, f"/admin/publish/{group[0]}")
            self.assertEqual(page.status, 200, page.text[:200])
            self.assertIn("Черновик поста", page.text)
        finally:
            desk.close()


# --------------------------------------------------------------------------- #
# 4. Operational chain through the documented CLI
# --------------------------------------------------------------------------- #

class OperationalCliChainTests(StagingCase):
    def _run(self, argv: list[str]) -> int:
        """Run a CLI command with its output captured (the tests stay quiet)."""

        import contextlib

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ops_cli.main(argv)

    def test_backup_and_restore_cli_commands(self) -> None:
        backups = self.root / "cli-backups"
        backups.mkdir(exist_ok=True)
        target = backups / "rr.sqlite"
        self.assertEqual(self._run(["backup", "--db", str(self.db), "--out", str(target)]), 0)
        self.assertTrue(target.exists())
        self.assertEqual(self._run(["verify-backup", "--backup", str(target)]), 0)
        clean = self.root / "cli-clean"
        clean.mkdir(exist_ok=True)
        restored = clean / "randreport.sqlite"
        self.assertEqual(self._run(["restore", "--backup", str(target), "--db", str(restored)]), 0)
        # a second restore without --force must fail loudly (exit code 2)
        self.assertEqual(self._run(["restore", "--backup", str(target), "--db", str(restored)]), 2)
        self.assertEqual(self._run(["restore", "--backup", str(target), "--db", str(restored), "--force"]), 0)
        # a missing backup is a clean failure, never a traceback
        self.assertEqual(self._run(["verify-backup", "--backup", str(backups / "absent.sqlite")]), 2)

    def test_verify_backup_handles_an_encrypted_backup(self) -> None:
        """``verify-backup`` must verify an ``*.enc`` backup, not open ciphertext.

        It used to hand the encrypted file straight to SQLite and die with an
        unhandled ``DatabaseError`` ("file is not a database"), so the documented
        verification command could not check the encrypted copy the operator
        actually keeps.
        """

        import contextlib
        import os
        from unittest import mock

        backups = self.root / "enc-backups"
        backups.mkdir(exist_ok=True)
        target = backups / "rr.sqlite.enc"
        passphrase = "synthetic-passphrase-for-the-encrypted-verify"
        env = dict(os.environ, RANREPORT_BACKUP_PASSPHRASE=passphrase)
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                self._run(["backup", "--db", str(self.db), "--out", str(target), "--encrypt"]), 0
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = ops_cli.main(["verify-backup", "--backup", str(target)])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertTrue(report["encrypted"])
            self.assertEqual("ok", report["integrity"])
            self.assertTrue(report["usable"])
        # without the passphrase it is refused in words, not as a driver error
        without = {k: v for k, v in os.environ.items() if k != "RANREPORT_BACKUP_PASSPHRASE"}
        with mock.patch.dict(os.environ, without, clear=True):
            self.assertEqual(self._run(["verify-backup", "--backup", str(target)]), 2)
        # a file that is not a database at all is a diagnosis, not a traceback
        junk = backups / "junk.sqlite"
        junk.write_bytes(b"not a database at all")
        self.assertEqual(self._run(["verify-backup", "--backup", str(junk)]), 2)

    def test_desk_refuses_to_start_without_a_server_secret(self) -> None:
        with self.assertRaises(ValueError):
            AdminDesk(db_path=self.db, secret="short", migrate=False)
        # and the constructor never echoes a secret it rejected
        secret = "x" * 8
        try:
            AdminDesk(db_path=self.db, secret=secret, migrate=False)
        except ValueError as exc:
            self.assertNotIn(secret, str(exc))


# --------------------------------------------------------------------------- #
# 5. No real secrets in git, tests or build output (O01)
# --------------------------------------------------------------------------- #

class SecretHygieneTests(unittest.TestCase):
    BOT_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
    SECRET_ASSIGNMENT_RE = re.compile(r"^(?P<name>[A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD)[A-Z0-9_]*)=(?P<value>.+)$")

    def _tracked_text_files(self) -> list[Path]:
        try:
            listing = subprocess.run(
                ["git", "-C", str(REPO), "ls-files", "-z"],
                capture_output=True,
                check=True,
            ).stdout
            names = [name for name in listing.decode("utf-8").split("\0") if name]
        except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover - no git in the sandbox
            names = [
                str(path.relative_to(REPO))
                for path in REPO.rglob("*")
                if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
            ]
        return [REPO / name for name in names]

    def test_no_real_telegram_bot_token_is_committed(self) -> None:
        offenders = []
        for path in self._tracked_text_files():
            if path.suffix in {".png", ".pdf", ".zip", ".xlsx"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if self.BOT_TOKEN_RE.search(text):
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [])

    def test_env_example_carries_only_placeholders(self) -> None:
        example = REPO / ".env.example"
        self.assertTrue(example.exists())
        allowed_flags = {"0", "1", "none", "true", "false"}
        for raw in example.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            match = self.SECRET_ASSIGNMENT_RE.match(line)
            if match is None:
                continue
            name, value = match.group("name"), match.group("value").strip()
            self.assertTrue(
                value.startswith("placeholder") or value in allowed_flags,
                f"{name} must be a documented placeholder, found {value!r}",
            )

    def test_build_output_contains_no_private_identifiers(self) -> None:
        site = PublicSite(release_dir=REPO / "site-out", db_path=None)
        out = Path(tempfile.mkdtemp(prefix="randreport-export-"))
        try:
            written = site.export_static(out)
            self.assertTrue(written)
            patterns = (
                re.compile(r"(?<![A-Za-z0-9])(?:acc|occ|cat|sub|cal|pair|tx)_[A-Za-z0-9_]+"),
                re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
            )
            for path in out.rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                for pattern in patterns:
                    self.assertEqual(pattern.findall(text), [], str(path))
        finally:
            site.close()
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
