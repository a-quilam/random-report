"""``web build`` must refuse a missing store or release path instead of writing a file.

Card t_3eeb34dc: ``python -m randreport.web build --periods 2025-07,2025`` run without
``--db``/``RANREPORT_DB`` used to create a file literally named ``None`` in the working
directory, because ``connect(None)`` was turned into the string ``"None"`` by
``str(path)``.  The clauses asserted here:

* a build with no database path (neither ``--db`` nor ``RANREPORT_DB``) exits 2 with a
  human sentence, prints no traceback and creates **no** file in the working directory;
* a build with no release directory (neither ``--release-dir`` nor
  ``RANREPORT_RELEASE_DIR``) is refused the same way, *before* the private store is
  opened or migrated - a missing release path must not mutate the store either;
* ``randreport.store.connect(None)`` and ``randreport.ops.publish.publish_release(None, ...)``
  refuse explicitly instead of inventing a path (defence in depth for every caller);
* with both paths supplied (argument or environment) the command still builds and
  publishes, so the refusal is about missing input, not about the command.

All paths are temporary directories; no real credential, source or store is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from randreport.ops.publish import PublishError, publish_release
from randreport.store import apply_migrations, connect
from randreport.web import __main__ as web_main


class WebBuildInputs(unittest.TestCase):
    """A build without its inputs is a refusal, never a stray ``None`` file."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="randreport-build-inputs-"))
        self.addCleanup(self._cleanup)
        self._cwd = Path.cwd()
        # every command runs in an empty directory: anything left behind is a defect
        self.work = self.tmp / "work"
        self.work.mkdir()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self._cwd)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers ---------------------------------------------------------- #

    def _run(self, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run the real CLI with every ``RANREPORT_*`` variable removed from the ambient env."""

        clean = {k: v for k, v in os.environ.items() if not k.startswith("RANREPORT_")}
        clean.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, clean, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = web_main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _migrated_store(self, path: Path) -> None:
        conn = connect(str(path))
        try:
            apply_migrations(conn)
        finally:
            conn.close()

    def _entries(self) -> list[str]:
        return sorted(p.name for p in self.work.iterdir())

    # --- the reported defect ---------------------------------------------- #

    def test_build_without_a_database_path_refuses_and_leaves_no_file_behind(self) -> None:
        code, _out, err = self._run(["build", "--periods", "2025-07,2025"])
        self.assertEqual(code, 2, err)
        self.assertIn("--db", err)
        self.assertIn("RANREPORT_DB", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self._entries(), [], "the refused build wrote a file into the working directory")

    def test_build_without_a_release_dir_refuses_before_touching_the_store(self) -> None:
        store = self.work / "store.sqlite"
        code, _out, err = self._run(["build", "--db", str(store), "--periods", "2025-07"])
        self.assertEqual(code, 2, err)
        self.assertIn("--release-dir", err)
        self.assertIn("RANREPORT_RELEASE_DIR", err)
        self.assertNotIn("Traceback", err)
        # the store was never opened, far less built into
        self.assertEqual(self._entries(), [])

    def test_build_with_a_release_dir_but_no_store_refuses_and_creates_no_release(self) -> None:
        releases = self.work / "releases"
        code, _out, err = self._run(["build", "--release-dir", str(releases), "--periods", "2025-07"])
        self.assertEqual(code, 2, err)
        self.assertIn("--db", err)
        self.assertEqual(self._entries(), [])

    def test_the_refusal_is_a_human_sentence_not_an_argparse_or_python_error(self) -> None:
        code, _out, err = self._run(["build", "--periods", "2025-07"])
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "a private store path is required (--db or RANREPORT_DB)")
        self.assertNotIn("Error", err)
        self.assertNotIn("Traceback", err)

    def test_an_unsupported_period_is_refused_without_a_traceback(self) -> None:
        store = self.work / "store.sqlite"
        releases = self.work / "releases"
        self._migrated_store(store)
        code, _out, err = self._run(
            ["build", "--db", str(store), "--release-dir", str(releases), "--periods", "nonsense"]
        )
        self.assertEqual(code, 2, err)
        self.assertIn("unsupported period", err)
        self.assertNotIn("Traceback", err)
        # refused before the store was read and before any release directory appeared
        self.assertEqual(self._entries(), ["store.sqlite"])

    # --- the documented way in still works -------------------------------- #

    def test_build_still_publishes_from_the_environment_paths(self) -> None:
        store = self.work / "store.sqlite"
        releases = self.work / "releases"
        self._migrated_store(store)
        code, out, err = self._run(
            ["build", "--periods", "2025-07"],
            env={"RANREPORT_DB": str(store), "RANREPORT_RELEASE_DIR": str(releases)},
        )
        self.assertEqual(code, 0, err)
        published = json.loads(out.strip().splitlines()[-1])["published"]
        self.assertEqual([label for label, _ in published], ["2025-07"])
        release_id = published[0][1]
        self.assertTrue((releases / release_id / "manifest.json").exists())

    def test_build_still_publishes_from_explicit_arguments(self) -> None:
        store = self.work / "store.sqlite"
        releases = self.work / "releases"
        self._migrated_store(store)
        code, out, err = self._run(
            ["build", "--db", str(store), "--release-dir", str(releases), "--periods", "2025-07"]
        )
        self.assertEqual(code, 0, err)
        published = json.loads(out.strip().splitlines()[-1])["published"]
        self.assertEqual(published[0][0], "2025-07")

    def test_an_explicit_argument_wins_over_the_environment(self) -> None:
        store = self.work / "store.sqlite"
        other = self.work / "other.sqlite"
        releases = self.work / "releases"
        self._migrated_store(store)
        code, out, err = self._run(
            ["build", "--db", str(store), "--periods", "2025-07"],
            env={"RANREPORT_DB": str(other), "RANREPORT_RELEASE_DIR": str(releases)},
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(store.exists() and store.stat().st_size > 0)
        self.assertFalse(other.exists(), "the argument did not win over RANREPORT_DB")


class MissingPathsAreRefusedInTheLibrary(unittest.TestCase):
    """``None`` is not a path: the store and the publisher say so themselves."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="randreport-build-inputs-"))
        self.addCleanup(self._rmtree)

    def _rmtree(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_connect_refuses_a_missing_path_and_creates_no_file(self) -> None:
        cwd = Path.cwd()
        os.chdir(self.tmp)
        try:
            with self.assertRaises(ValueError) as caught:
                connect(None)
            self.assertIn("path", str(caught.exception).lower())
            self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [])
        finally:
            os.chdir(cwd)

    def test_publish_release_refuses_a_missing_root(self) -> None:
        with self.assertRaises(PublishError) as caught:
            publish_release(None, "rel_x", {"manifest.json": "{}"})
        self.assertIn("release directory", str(caught.exception))
        self.assertFalse(Path("None").exists())

    def test_latest_published_release_refuses_a_missing_root(self) -> None:
        from randreport.ops.publish import latest_published_release

        with self.assertRaises(PublishError):
            latest_published_release(None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
