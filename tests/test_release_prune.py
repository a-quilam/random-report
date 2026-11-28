"""A rebuild of a period replaces its published release — it never leaves a stale one.

Release ids are content hashes, so re-building a period after a code or map
change publishes a *new* directory and the previous one stays on disk.  Two
releases for one period both pass the allowlist, and the site could then serve
the stale projection (for example the earlier, more detailed money rows).  This
pins the two guards added for card t_1b5b08c8:

* ``prune_superseded_releases`` deletes the same-period directory that the new
  build replaces (and only that one, and never the ``current`` symlink);
* ``ReleaseIndex.by_period`` answers deterministically — the current release when
  it covers the period, otherwise the newest directory — instead of by name,
  which carries no order.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.ops.publish import prune_superseded_releases  # noqa: E402
from randreport.web.release_index import ReleaseIndex  # noqa: E402
from tests import dev_release  # noqa: E402


class ReleasePruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.releases = base / "releases"
        self.db = str(base / "randreport.sqlite")
        built = dev_release.build(str(self.releases), period="2025-07", db_path=self.db)
        self.release_id = built["release_id"]
        self.assertTrue((self.releases / self.release_id / "money.json").exists())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _stale_clone(self, name: str = "rel_staleclone") -> Path:
        clone = self.releases / name
        shutil.copytree(self.releases / self.release_id, clone)
        # make it strictly newer so a modification-time pick would choose it
        future = time.time() + 30
        os.utime(clone, (future, future))
        os.utime(clone / "money.json", (future, future))
        return clone

    def test_by_period_prefers_the_current_release_over_a_newer_stale_clone(self) -> None:
        clone = self._stale_clone()
        index = ReleaseIndex(self.releases)
        self.assertEqual(self.release_id, index.current_id())
        chosen = index.by_period("2025-07")
        if chosen is None:
            self.fail("the period resolve returned nothing")
        self.assertEqual(self.release_id, chosen.release_id)
        self.assertNotEqual(clone.name, chosen.release_id)

    def test_prune_removes_the_same_period_clone_and_keeps_the_current_one(self) -> None:
        clone = self._stale_clone()
        removed = prune_superseded_releases(self.releases, period_label="2025-07", keep_id=self.release_id)
        self.assertEqual(1, removed)
        self.assertFalse(clone.exists())
        self.assertTrue((self.releases / self.release_id).is_dir())
        self.assertEqual(self.release_id, os.readlink(self.releases / "current"))

    def test_prune_leaves_another_period_alone(self) -> None:
        other = dev_release.build(str(self.releases), period="2025-06", db_path=self.db)
        other_id = other["release_id"]
        self.assertNotEqual(self.release_id, other_id)
        removed = prune_superseded_releases(self.releases, period_label="2025-07", keep_id=self.release_id)
        self.assertEqual(0, removed)
        self.assertTrue((self.releases / other_id).is_dir())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
