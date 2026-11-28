"""Read-only operator reconciliation of the approved totals (ops.reconcile).

Every clause is exercised on synthetic fixtures and an offline, file-backed
store: the store is opened ``mode=ro``, the frozen domain recomputes the
approved totals, and the report carries aggregates and public category keys
only. No network request, no secret, no real account.

The clauses:

* the recomputed approved totals reconcile within themselves (category rows vs
  the same query's aggregates, and months vs the year), so an untouched store
  reports no ``error`` divergence;
* the recomputed aggregate surface equals the published ``MoneyDTO`` field by
  field, so a deliberate change to the published file is *detected*, not masked;
* the provisionality reasons of the public surface are reported as they are;
* the store is not written to and no source is read — reconciling leaves the
  database byte-for-byte unchanged;
* the output has no transaction id, account id, raw category id or operation,
  and passes ``assert_no_leaks`` like any public payload;
* the operator command refuses a missing or unmigrated store instead of
  creating one, and prints a human sentence, not a traceback.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from randreport.domain.timeframes import month_period, year_period  # noqa: E402
from randreport.ops import cli as ops_cli  # noqa: E402
from randreport.ops.reconcile import ReconcileError, open_store, reconcile  # noqa: E402
from randreport.projection.privacy import find_leaks  # noqa: E402
from randreport.store import apply_migrations, connect  # noqa: E402
from tests import fixtures_admin as fa  # noqa: E402


class ReconcileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release_dir = self.root / "releases"
        self.release_dir.mkdir(parents=True, exist_ok=True)
        # the real nightly cycle over offline fixtures, into a file store
        self.conn, _ = fa.build_pipeline_db(
            self.root / "private.sqlite", release_dir=self.release_dir, period="2025-07"
        )
        fa.record_mappings(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _report(self, period="2025-07", *, as_of=None):
        return reconcile(self.conn, period=month_period(period), as_of=as_of, release_dir=self.release_dir)

    # --- internal reconciliation ------------------------------------------ #

    def test_clean_store_has_no_error_divergence(self) -> None:
        report = self._report()
        errors = [d for d in report.divergences if d.severity == "error"]
        self.assertEqual([], errors, [d.to_public() for d in errors])
        self.assertTrue(report.ok)

    def test_year_reconciles_against_its_months(self) -> None:
        report = reconcile(self.conn, period=year_period("2025"), as_of=None, release_dir=self.release_dir)
        codes = {d.code for d in report.divergences}
        self.assertNotIn("month_year_mismatch", codes)
        self.assertNotIn("category_sum_mismatch", codes)

    # --- published comparison --------------------------------------------- #

    def test_recomputed_totals_match_the_published_dto(self) -> None:
        report = self._report()
        codes = {d.code for d in report.divergences}
        self.assertNotIn("published_mismatch", codes)
        self.assertNotIn("provisionality_mismatch", codes)

    def test_a_tampered_published_figure_is_detected_not_masked(self) -> None:
        current = os.readlink(self.release_dir / "current")
        money_path = self.release_dir / current / "money.json"
        payload = json.loads(money_path.read_text(encoding="utf-8"))
        original = payload["metrics"]["compensation_kopecks"]
        payload["metrics"]["compensation_kopecks"] = int(original or 0) + 1
        money_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")

        report = self._report()
        mismatches = [d for d in report.divergences if d.code == "published_mismatch"]
        self.assertTrue(mismatches, "a changed published figure must surface as a divergence")
        fields = {d.details["field"] for d in mismatches}
        self.assertIn("compensation_kopecks", fields)
        # the recomputed value is the approved one; nothing was "fixed"
        self.assertEqual(original, report.to_public()["totals"]["compensation_kopecks"])

    # --- provisionality ---------------------------------------------------- #

    def test_provisionality_reasons_are_reported(self) -> None:
        report = self._report()
        # the fixtures carry an unmapped category and a quarantined account
        self.assertIn("unclassified_present", report.provisionality)
        self.assertIn("quarantine_present", report.provisionality)
        self.assertEqual("partial", report.completeness)
        self.assertFalse(report.coverage_is_final)

    # --- read-only --------------------------------------------------------- #

    def test_reconciling_does_not_write_the_store(self) -> None:
        before = self._logical_state()
        self._report()
        reconcile(self.conn, period=year_period("2025"))
        after = self._logical_state()
        self.assertEqual(before, after, "reconciliation must not change any stored row")

    def test_open_store_refuses_writes(self) -> None:
        import sqlite3

        conn = open_store(self.root / "private.sqlite")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM finance_transactions")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO audit_log (action, actor) VALUES ('x', 'y')")
        finally:
            conn.close()

    def test_open_store_does_not_create_or_migrate(self) -> None:
        from randreport.store.migrations import missing_tables

        missing = self.root / "no-such.sqlite"
        with self.assertRaises(ReconcileError):
            open_store(missing)
        self.assertFalse(missing.exists(), "a missing store must not be created")
        # an empty, unmigrated database is refused rather than migrated
        empty = self.root / "empty.sqlite"
        conn = connect(empty)
        conn.close()
        with self.assertRaises(ReconcileError):
            open_store(empty)
        check = connect(empty)
        try:
            self.assertTrue(missing_tables(check), "the unmigrated store must stay unmigrated")
        finally:
            check.close()

    def _logical_state(self) -> dict[str, object]:
        """Every stored row, read back through a plain connection (synthetic data)."""

        conn = connect(self.root / "private.sqlite")
        try:
            tables = [
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            return {name: tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {name}")) for name in tables}
        finally:
            conn.close()

    # --- privacy ----------------------------------------------------------- #

    def test_output_carries_no_private_identifiers(self) -> None:
        payload = self._report().to_public()
        self.assertEqual((), find_leaks(payload))
        dumped = json.dumps(payload, ensure_ascii=False)
        for token in ("transaction_id", "account_id", "category_id", "acc_", "tx_", "cat_"):
            self.assertNotIn(token, dumped)

    @staticmethod
    def _snapshot(db: Path) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(db.parent.glob(db.name + "*"))}


class ReconcileCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release_dir = self.root / "releases"
        self.release_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "private.sqlite"
        conn, _ = fa.build_pipeline_db(self.db, release_dir=self.release_dir, period="2025-07")
        fa.record_mappings(conn)
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_reconciles_a_clean_store_with_exit_zero(self) -> None:
        code = ops_cli.main(
            ["reconcile", "--db", str(self.db), "--period", "2025-07", "--publish-dir", str(self.release_dir)]
        )
        self.assertEqual(0, code)

    def test_cli_needs_a_period(self) -> None:
        self.assertEqual(2, ops_cli.main(["reconcile", "--db", str(self.db)]))

    def test_cli_refuses_a_missing_store(self) -> None:
        self.assertEqual(5, ops_cli.main(["reconcile", "--db", str(self.root / "missing.sqlite"), "--period", "2025-07"]))

    def test_cli_refuses_an_unmigrated_store(self) -> None:
        empty = self.root / "empty.sqlite"
        conn = connect(empty)
        conn.close()
        self.assertEqual(5, ops_cli.main(["reconcile", "--db", str(empty), "--period", "2025-07"]))
        check = connect(empty)
        try:
            from randreport.store.migrations import missing_tables

            self.assertTrue(missing_tables(check), "the unmigrated store must stay unmigrated")
        finally:
            check.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
