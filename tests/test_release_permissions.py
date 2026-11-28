"""A published release must be readable by the public web server.

``publish_release`` builds the release inside a ``tempfile.mkdtemp`` directory,
which is created with mode 0700.  Moving that directory into place with
``os.replace`` publishes the 0700 with it: the web server running as another
user can then not enter the release directory at all (card t_1c024aa9).  The
published tree must carry public modes — 0755 on directories, 0644 on files —
independently of the process umask.
"""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.ops.publish import publish_release  # noqa: E402


class ReleasePermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "releases"

    def test_published_release_is_world_readable(self) -> None:
        publish_release(
            self.base,
            "rel_permcheck",
            {
                "money.json": "{}",
                "index.html": "<html></html>",
            },
        )

        release = self.base / "rel_permcheck"
        self.assertTrue(release.is_dir())

        self.assertEqual(
            stat.S_IMODE(release.stat().st_mode),
            0o755,
            "the release directory must be 0755, not the mkdtemp 0700",
        )
        for name in ("money.json", "index.html", "manifest.json"):
            mode = stat.S_IMODE((release / name).stat().st_mode)
            self.assertEqual(
                mode,
                0o644,
                f"published file {name!r} must be 0644, got {oct(mode)}",
            )


if __name__ == "__main__":
    unittest.main()
