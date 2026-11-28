"""Store contract: migrations from a clean database, keys, and leak-checked releases.

docs/REQUIREMENTS.md section 10 (data model and migrations) with the section 5
rule that secrets come only from the environment.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.projection.privacy import find_leaks  # noqa: E402
from randreport.store import apply_migrations, connect, missing_tables, schema_version  # noqa: E402
from randreport.store.db import transaction  # noqa: E402
from randreport.store.migrations import MIGRATIONS, REQUIRED_TABLES  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402


class TestMigrations(unittest.TestCase):
    def test_a_clean_database_gets_every_required_table(self):
        conn = connect()
        applied = apply_migrations(conn)
        # the expected versions come from the migration table itself, so adding a
        # migration does not require editing this contract test
        self.assertEqual(tuple(migration.version for migration in MIGRATIONS), applied)
        self.assertEqual((), missing_tables(conn))
        self.assertTrue(REQUIRED_TABLES.issubset({row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}))
        self.assertEqual(max(migration.version for migration in MIGRATIONS), schema_version(conn))
        conn.close()

    def test_migrations_are_idempotent(self):
        conn = connect()
        apply_migrations(conn)
        self.assertEqual((), apply_migrations(conn))
        self.assertEqual(max(migration.version for migration in MIGRATIONS), schema_version(conn))
        conn.close()

    def test_an_existing_database_only_gets_the_pending_migrations(self):
        conn = connect()
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        first = MIGRATIONS[0]
        with transaction(conn):
            for statement in first.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (first.version, first.name, "2025-07-31T00:00:00+00:00"),
            )
        pending = tuple(migration.version for migration in MIGRATIONS if migration.version > first.version)
        self.assertEqual(pending, apply_migrations(conn, now="2025-08-01T00:00:00+00:00"))
        self.assertEqual(max(migration.version for migration in MIGRATIONS), schema_version(conn))
        conn.close()

    def test_foreign_keys_are_enforced(self):
        conn = connect()
        apply_migrations(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO finance_allocations_or_classification (transaction_id, economic_class, mapping_version)"
                " VALUES ('missing', 'operating_expense', 'v1')"
            )
        conn.close()

    def test_a_source_run_is_unique_per_source_and_logical_day(self):
        conn = connect()
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO source_runs (run_id, source, logical_date, cutoff, started_at, status, last_attempt_at)"
            " VALUES ('r1', 'zenmoney', '2025-07-31', 'x', 'x', 'running', 'x')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_runs (run_id, source, logical_date, cutoff, started_at, status, last_attempt_at)"
                " VALUES ('r2', 'zenmoney', '2025-07-31', 'x', 'x', 'running', 'x')"
            )
        conn.close()

    def test_the_database_file_uses_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "randreport.sqlite")
            self.assertEqual("wal", conn.execute("PRAGMA journal_mode").fetchone()[0])
            conn.close()


class TestReleaseContract(unittest.TestCase):
    def test_a_stored_release_is_the_leak_checked_public_json(self):
        conn = ig.fresh_conn()
        tmp = tempfile.TemporaryDirectory()
        try:
            client, _ = ig.zenmoney(rows=[ig.zen_row("t1", "2025-07-05", 100_000, account="acc_card", tag=["cat_rent"])])
            ig.run_once(conn, [ig.zenmoney_adapter(client)], lease=str(Path(tmp.name) / "lock"))
            payload = conn.execute("SELECT payload FROM public_releases WHERE is_current = 1").fetchone()["payload"]
            self.assertEqual((), find_leaks(json.loads(payload)))
            self.assertNotIn("acc_card", payload)
            self.assertNotIn("transaction_id", payload)
        finally:
            tmp.cleanup()
            conn.close()

    def test_a_release_is_never_rewritten(self):
        conn = ig.fresh_conn()
        tmp = tempfile.TemporaryDirectory()
        try:
            client, _ = ig.zenmoney(rows=[ig.zen_row("t1", "2025-07-05", 100_000, account="acc_card", tag=["cat_rent"])])
            report = ig.run_once(conn, [ig.zenmoney_adapter(client)], lease=str(Path(tmp.name) / "lock"))
            from randreport.store import repository as repo

            self.assertEqual("unchanged", repo.store_release(
                conn,
                release_id=report.release_id,
                as_of="2025-07-31T00:00:00+03:00",
                schema_version="public-release-1",
                sections=("money",),
                payload_text="{}",
            ))
            stored = conn.execute("SELECT payload FROM public_releases WHERE release_id = ?", (report.release_id,)).fetchone()
            self.assertNotEqual("{}", stored["payload"])
        finally:
            tmp.cleanup()
            conn.close()


class TestSecretsHygiene(unittest.TestCase):
    def test_env_example_holds_placeholders_only(self):
        path = ROOT / ".env.example"
        self.assertTrue(path.exists())
        secret_keys = ("TOKEN", "KEY", "SECRET", "PASSWORD", "API_KEY")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            self.assertTrue(value.strip(), line)
            if any(token in name.upper() for token in secret_keys):
                self.assertTrue(
                    value.startswith("placeholder") or value in ("none", ""),
                    f"{name} must not carry a real secret: {line}",
                )

    def test_env_files_are_ignored_but_the_example_is_tracked(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env*", ignore)
        self.assertIn("!.env.example", ignore)


if __name__ == "__main__":
    unittest.main()
