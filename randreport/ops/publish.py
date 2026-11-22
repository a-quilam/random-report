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
from typing import Any, Mapping

CURRENT = "current"


class PublishError(RuntimeError):
    """A publication that could not be completed atomically."""


def _release_root(root: str | Path | None) -> Path:
    """The release directory, refused when the caller passed no usable path.

    ``None`` is not a path: ``Path(None)`` raises a bare ``TypeError`` deep inside the
    publisher, and a plain ``str()`` would create a directory named ``None`` next to the
    working directory. Both are refused here as one human sentence (card t_3eeb34dc).
    """

    if root is None:
        raise PublishError("a release directory is required")
    if not isinstance(root, (str, os.PathLike)):
        raise PublishError(f"a release directory must be a string or a path, not {root!r}")
    return Path(root)


def publish_release(
    root: str | Path | None,
    release_id: str,
    files: Mapping[str, str],
    *,
    link_name: str = CURRENT,
) -> Path:
    """Write ``files`` as release ``release_id`` and atomically point ``current`` at it."""

    base = _release_root(root)

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


def latest_published_release(root: str | Path | None, *, link_name: str = CURRENT) -> str | None:
    link = _release_root(root) / link_name
    if not link.is_symlink() and not link.exists():
        return None
    return os.readlink(link)


def prune_superseded_releases(root: str | Path | None, *, period_label: str, keep_id: str) -> int:
    """Delete older published releases that cover the same period as ``keep_id``.

    A release id is a content hash, so re-building a period after a code or map
    change creates a *new* directory and the previous one is left behind.  Two
    releases for one period would both pass the allowlist, and the public site
    would then be able to serve the stale one (``by_period`` used to pick by name,
    which has no order).  The superseded directory is removed here so what is
    published for a period is always exactly the newest build, and the stale
    projection cannot leak from the disk.  The current symlink is never touched.
    """

    base = _release_root(root)
    if not base.is_dir():
        return 0
    removed = 0
    for entry in sorted(base.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not entry.name.startswith("rel_"):
            continue
        if entry.name == keep_id:
            continue
        money = entry / "money.json"
        try:
            payload = json.loads(money.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # a broken sibling is not a same-period release we can prove safe to
            # delete; leave it (the allowlist will refuse it when it is read)
            continue
        period = payload.get("period")
        requested = period.get("requested") if isinstance(period, dict) else None
        if requested == period_label:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def previous_published_release(root: str | Path | None, *, link_name: str = CURRENT) -> str | None:
    """The release published just before the current one, if any.

    Release ids are content hashes, so they carry no order: the publication order
    has to come from the file system's modification time of each release
    directory (ties broken by name). Rollback must land on a *real, already
    published* revision, never on a freshly built one (docs/DEPLOYMENT.md §7).
    """

    base = _release_root(root)
    if not base.is_dir():
        return None
    entries = [
        entry
        for entry in base.iterdir()
        if entry.is_dir() and not entry.name.startswith(".") and entry.name.startswith("rel_")
    ]
    order = [entry.name for entry in sorted(entries, key=lambda p: (p.stat().st_mtime, p.name))]
    if not order:
        return None
    current = latest_published_release(base, link_name=link_name)
    if current is None or current not in order:
        return order[-1]
    index = order.index(current)
    return order[index - 1] if index > 0 else None


def rollback_release(root: str | Path | None, release_id: str, *, link_name: str = CURRENT) -> dict[str, Any]:
    """Atomically point ``link_name`` back at an existing, verified release.

    The target is loaded through the public allowlist (:class:`ReleaseIndex`)
    before the pointer moves, so a directory that is missing, corrupt or carries a
    field outside the frozen allowlist is refused instead of published. After the
    single atomic ``os.replace`` the pointer is read back and the release is
    loaded again, which is the verification a rollback has to carry: the caller
    gets the id it asked for, the id it left, and the sections that are now live.

    Rollback never rewrites a release: it moves a symlink between revisions that
    already exist (invariant R01).
    """

    from ..web.release_index import ReleaseIndex  # local import: keeps ops free of web at import time

    base = _release_root(root)
    if not base.is_dir():
        raise PublishError(f"publish directory does not exist: {base}")
    name = str(release_id)
    if "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise PublishError(f"invalid release id {release_id!r}")
    target = base / name
    if not target.is_dir():
        raise PublishError(f"release {name!r} is not present in {base}")

    index = ReleaseIndex(base)
    try:
        release = index.get(name)
    except Exception as exc:  # noqa: BLE001 - any unreadable release is a refusal, not a crash
        raise PublishError(f"release {name!r} could not be loaded; refusing to roll back to it: {exc}") from exc
    if release is None:
        raise PublishError(f"release {name!r} could not be loaded; refusing to roll back to it")

    previous = latest_published_release(base, link_name=link_name)

    temp_link = base / f".{link_name}.tmp-{os.getpid()}"
    if temp_link.exists() or temp_link.is_symlink():
        temp_link.unlink()
    os.symlink(name, temp_link)
    os.replace(temp_link, base / link_name)

    # verification: the pointer moved and the release on the other side loads
    landed = latest_published_release(base, link_name=link_name)
    verified = landed == name and index.current() is not None
    return {
        "release_id": name,
        "previous": previous,
        "verified": verified,
        "sections": sorted(release.sections),
    }

