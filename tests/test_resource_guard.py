"""The automatic pre-exit leak check has to be checkable itself.

Each test drives a real process or a real handle: the guard is only worth
anything if it fails on a process that leaked, stays quiet on one that closed
everything, and never interferes with the warning Python emits by itself.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from randreport import leakguard
from randreport.store import connect


def run_script(source: str, *, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a throwaway script inside the repository tree and return its result."""

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "leak_script.py"
        script.write_text(source, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )


class OpenResourceTests(unittest.TestCase):
    """open_resources() reads the real state of a handle."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(leakguard.close_tracked_handles)  # a deliberate leak never leaks out
        self.db = str(Path(self.tmp.name) / "guard.sqlite")

    def mine(self, kind: str = "sqlite connection") -> list[leakguard.OpenResource]:
        """The open handles this test file owns (other tests do not interfere)."""

        here = Path(__file__).name
        return [
            r for r in leakguard.open_resources() if r.kind == kind and here in r.created_at
        ]

    def test_unclosed_connection_is_reported_with_its_owner(self) -> None:
        before = len(self.mine())
        conn = connect(self.db)
        try:
            found = self.mine()
            self.assertEqual(len(found), before + 1, found)
            self.assertIn("tests/test_resource_guard.py", found[-1].created_at)
        finally:
            conn.close()
        self.assertEqual(len(self.mine()), before)

    def test_connection_dropped_without_close_is_still_reported(self) -> None:
        """A handle its owner simply dropped is the exact case the warning covers."""

        before = len(self.mine())
        connect(self.db)  # no reference kept, no close()
        self.assertEqual(len(self.mine()), before + 1)

    def test_closed_connection_is_not_reported(self) -> None:
        before = len(self.mine())
        conn = connect(self.db)
        conn.close()
        self.assertEqual(len(self.mine()), before)

    def test_connection_created_on_a_worker_thread_still_reads_as_open(self) -> None:
        """The trap that hid the editorial desk leak: a cross-thread probe raises
        ProgrammingError("threads"), which is not the same as "closed"."""

        created = threading.Event()
        release = threading.Event()
        box: list[sqlite3.Connection] = []
        failure: list[BaseException] = []
        before = len(self.mine())

        def worker() -> None:
            try:
                conn = connect(self.db)  # check_same_thread=True on purpose
                box.append(conn)
                created.set()
                release.wait(10)
                conn.close()
            except BaseException as exc:  # noqa: BLE001 - reported in the main thread
                failure.append(exc)
                created.set()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(created.wait(10))
        try:
            self.assertFalse(failure, failure)
            self.assertEqual(len(self.mine()), before + 1, "a handle owned by another thread is still open")
            with self.assertRaises(sqlite3.ProgrammingError) as ctx:
                box[0].execute("select 1")
            self.assertIn("thread", str(ctx.exception))
            self.assertNotIn("closed", str(ctx.exception))
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(failure, failure)
        self.assertEqual(len(self.mine()), before, "close() on the owning thread settles the guard")

    def test_listening_socket_is_reported_until_the_server_is_closed(self) -> None:
        import socketserver
        from http.server import HTTPServer

        class Quiet(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # pragma: no cover - never serves a request
                return

        before = len(self.mine("listening socket"))
        server = HTTPServer(("127.0.0.1", 0), Quiet)
        try:
            self.assertEqual(len(self.mine("listening socket")), before + 1, "an open listener is a leaked handle")
        finally:
            server.server_close()  # shutdown() alone leaves the socket open
        self.assertEqual(len(self.mine("listening socket")), before)

    def test_check_raises_with_the_open_handles(self) -> None:
        before = len(self.mine())
        conn = connect(self.db)
        try:
            with self.assertRaises(leakguard.ResourceLeakError) as ctx:
                leakguard.check()
            self.assertTrue(
                any("test_resource_guard.py" in r.created_at for r in ctx.exception.resources),
                ctx.exception.resources,
            )
        finally:
            conn.close()
        self.assertEqual(len(self.mine()), before)  # clean again


class InstalledGuardTests(unittest.TestCase):
    """The guard is installed for the suite and turns a leak into a failure."""

    def test_the_suite_installs_the_guard(self) -> None:
        self.assertTrue(leakguard.installed(), "tests/__init__.py must install the guard")

    def test_install_does_not_touch_the_warning_filters(self) -> None:
        before = list(warnings.filters)
        self.assertFalse(leakguard.install())  # already installed by tests/__init__.py
        self.assertEqual(list(warnings.filters), before)

    def test_install_is_idempotent(self) -> None:
        self.assertFalse(leakguard.install())

    def test_a_leaking_process_exits_non_zero_with_a_report(self) -> None:
        done = run_script(
            "import sqlite3\n"
            "from randreport.leakguard import install\n"
            "install(verbose=True)\n"
            "sqlite3.connect(':memory:')  # dropped without close()\n"
            "print('script done')\n"
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("resource leak: 1 handle(s) are still open before exit", done.stderr)
        self.assertIn("sqlite connection", done.stderr)
        self.assertIn("script done", done.stdout)

    def test_a_clean_process_exits_zero_and_confirms(self) -> None:
        done = run_script(
            "from randreport.leakguard import install\n"
            "install(verbose=True)\n"
            "from randreport.store import connect\n"
            "conn = connect(':memory:')\n"
            "conn.close()\n"
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("resource guard: no unclosed sqlite connection", done.stderr)
        self.assertNotIn("resource leak:", done.stderr)

    def test_the_exit_code_is_configurable(self) -> None:
        done = run_script(
            "import sqlite3\n"
            "from randreport.leakguard import install\n"
            "install(exit_code=7)\n"
            "sqlite3.connect(':memory:')\n"
        )
        self.assertEqual(done.returncode, 7, done.stderr)

    def test_a_leak_still_prints_pythons_own_warning_trail(self) -> None:
        """The check adds a failure; it never replaces or silences the warning."""

        done = run_script(
            "import sqlite3\n"
            "from randreport.leakguard import install\n"
            "install()\n"
            "sqlite3.connect(':memory:')\n",
            extra_env={"PYTHONWARNINGS": "always::ResourceWarning"},
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("ResourceWarning", done.stderr)
        self.assertIn("unclosed database", done.stderr)

    def test_a_leaking_listener_is_caught_too(self) -> None:
        done = run_script(
            "import socketserver\n"
            "from http.server import HTTPServer\n"
            "from randreport.leakguard import install\n"
            "install()\n"
            "class H(socketserver.BaseRequestHandler):\n"
            "    def handle(self):\n"
            "        pass\n"
            "server = HTTPServer(('127.0.0.1', 0), H)  # built, never closed\n"
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("listening socket", done.stderr)

    def test_the_maintenance_cli_installs_the_guard(self) -> None:
        """Every `python -m randreport…` command carries the check into production."""

        done = run_script(
            "import sys\n"
            "from randreport import leakguard\n"
            "import randreport.web.__main__ as web_main\n"
            "print('installed before main():', leakguard.installed())\n"
            "try:\n"
            "    web_main.main(['seed', '--db', ':memory:'])\n"
            "except SystemExit:\n"
            "    pass\n"
            "print('installed after main():', leakguard.installed())\n"
        )
        self.assertIn("installed after main(): True", done.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
