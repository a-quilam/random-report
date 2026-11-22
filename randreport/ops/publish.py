"""Atomic publication of a public release on disk.

The whole release is written under a temporary directory and moved into place with
one ``os.replace``; the ``current`` pointer is then swapped with a single atomic
rename of a symlink. A reader therefore sees either the previous release or the
new one, never a half-written tree. An existing release id is never rewritten.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

CURRENT = "current"


class PublishError(RuntimeError):
    """A publication that could not be completed atomically."""


def publish_release(
    root: str | Path,
    release_id: str,
    files: Mapping[str, str],
    *,
    link_name: str = CURRENT,
) -> Path:
    """Write ``files`` as release ``release_id`` and atomically point ``current`` at it."""

    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    target = base / release_id
    if not target.exists():
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{release_id}.", dir=base))
        try:
            for name, content in files.items():
                if "/" in name or "\\" in name or name in (".", ".."):
                    raise PublishError(f"invalid release file name {name!r}")
                (temp_dir / name).write_text(content, encoding="utf-8")
            manifest = {"release_id": release_id, "files": sorted(files)}
            (temp_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            os.replace(temp_dir, target)
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    temp_link = base / f".{link_name}.tmp-{os.getpid()}"
    if temp_link.exists() or temp_link.is_symlink():
        temp_link.unlink()
    os.symlink(release_id, temp_link)
    os.replace(temp_link, base / link_name)
    return target


def latest_published_release(root: str | Path, *, link_name: str = CURRENT) -> str | None:
    link = Path(root) / link_name
    if not link.is_symlink() and not link.exists():
        return None
    return os.readlink(link)
