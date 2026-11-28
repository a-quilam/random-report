"""The operator token command must store the secret without touching anything else.

Card: «Дай команду в терминал, которой я могу дать токен tgstat». The command is
the only place where an operator secret enters the contour, so its guarantees are
pinned here: the value never comes from the command line, no other setting in the
service environment file changes, the file stays private, a repeated run does not
grow a second token line, and a nonsense value is refused instead of written.
"""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "enter-tgstat-token.py"
_spec = importlib.util.spec_from_file_location("enter_tgstat_token", SCRIPT)
assert _spec and _spec.loader
token_cmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(token_cmd)

GOOD = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
BASE = (
    "RANREPORT_DB=/var/lib/randreport/randreport.sqlite\n"
    "TELEGRAM_BOT_TOKEN=placeholder-bot-token\n"
    "TELEGRAM_PUBLIC_CHANNEL=@raporto_ai\n"
)


class TestTheTokenCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = Path(self.dir.name) / "randreport.env"
        self.file.write_text(BASE, encoding="utf-8")
        os.chmod(self.file, 0o600)

    def run_cmd(self, value: str) -> int:
        import contextlib
        import sys

        saved = sys.stdin
        sys.stdin = io.StringIO(value + "\n")
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                return token_cmd.main(["--stdin", "--file", str(self.file)])
        finally:
            sys.stdin = saved

    def test_a_nonsense_value_is_refused_and_nothing_is_written(self) -> None:
        before = self.file.read_bytes()
        self.assertEqual(2, self.run_cmd("не токен"))
        self.assertEqual(before, self.file.read_bytes())

    def test_the_secret_is_stored_once_and_every_other_setting_survives(self) -> None:
        self.assertEqual(0, self.run_cmd(GOOD))
        text = self.file.read_text(encoding="utf-8")
        self.assertIn(f"TGSTAT_TOKEN={GOOD}\n", text)
        self.assertIn("TELEGRAM_BOT_TOKEN=placeholder-bot-token\n", text)
        self.assertIn("RANREPORT_DB=/var/lib/randreport/randreport.sqlite\n", text)
        # the counters belong to the channel the bot already knows
        self.assertIn("TGSTAT_CHANNEL=@raporto_ai\n", text)
        self.assertEqual(1, text.count("TGSTAT_TOKEN="))
        self.assertEqual(0o600, stat.S_IMODE(self.file.stat().st_mode))

    def test_a_second_run_does_not_grow_a_second_token_line(self) -> None:
        self.assertEqual(0, self.run_cmd(GOOD))
        self.assertEqual(0, self.run_cmd("f" * 32))
        text = self.file.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("TGSTAT_TOKEN="))
        self.assertIn("TGSTAT_TOKEN=" + "f" * 32 + "\n", text)
        self.assertEqual(1, text.count("TGSTAT_CHANNEL="))

    def test_a_missing_file_is_refused_instead_of_created(self) -> None:
        missing = Path(self.dir.name) / "absent.env"
        import sys

        saved = sys.stdin
        sys.stdin = io.StringIO(GOOD + "\n")
        try:
            self.assertEqual(2, token_cmd.main(["--stdin", "--file", str(missing)]))
        finally:
            sys.stdin = saved
        self.assertFalse(missing.exists())

    def test_the_confirmation_never_repeats_the_secret(self) -> None:
        import contextlib
        import sys

        saved, buf = sys.stdin, io.StringIO(GOOD + "\n")
        out = io.StringIO()
        sys.stdin = buf
        try:
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, token_cmd.main(["--stdin", "--file", str(self.file)]))
        finally:
            sys.stdin = saved
        printed = out.getvalue()
        self.assertNotIn(GOOD, printed)
        self.assertIn("TGSTAT_TOKEN", printed)


if __name__ == "__main__":
    unittest.main()
