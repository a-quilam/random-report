"""Automatic pre-exit check for unclosed process resources.

The acceptance run of the public contour surfaced a class of defect that Python
only *reports* and never fails on: a SQLite handle, a listening socket or an HTTP
response that is finalised without being closed emits a ``ResourceWarning``, and
that is all -- ``python -W error::ResourceWarning -m unittest discover`` still
ends ``OK`` with exit code 0, because the warning is raised inside a finaliser
where it is printed as an unraisable exception instead of being raised into the
test.  A human has to notice the trail on stderr, so the defect comes back the
next time somebody opens a connection and forgets to close it.

This module turns that trail into a hard check that runs before the process
exits:

* :func:`open_resources` returns every connection, listening socket and HTTP
  response that is still open, naming the site that created it;
* :func:`check` raises :class:`ResourceLeakError` unless that set is empty;
* :func:`install` registers the check as an ``atexit`` hook, so a process that
  leaked anything prints a report and exits non-zero, while a clean process
  prints one confirmation line when ``verbose`` is set.

Rules this module follows deliberately:

* nothing here filters, suppresses or downgrades a warning: ``TrackedConnection``
  defines no ``__del__`` (a Python finaliser would replace the C finaliser that
  emits ``unclosed database``) and the registry holds the handles instead, and
  because the check exits with ``os._exit`` -- skipping the finalisation that
  would print the warnings -- the guard emits the equivalent ``ResourceWarning``
  itself first.  A test asserts that the trail *and* the failure appear for the
  same handle;
* the check is *non-destructive*: it never closes a handle itself, because
  closing what an owner forgot to close would hide the defect instead of
  surfacing it;
* closedness is read from the handle, never guessed.  The per-thread guard makes
  a cross-thread probe raise ``ProgrammingError`` for an open *and* a closed
  connection alike -- the exact misreading that hid the editorial desk's leaked
  handle -- so connections come back as :class:`TrackedConnection`, which records
  its own ``close()``.
"""

from __future__ import annotations

import atexit
import gc
import http.client
import os
import socket
import sqlite3
import socketserver
import sys
import traceback
import warnings
from dataclasses import dataclass
from typing import Iterable, TextIO

__all__ = [
    "OpenResource",
    "ResourceLeakError",
    "TrackedConnection",
    "open_resources",
    "check",
    "close_tracked_handles",
    "report",
    "install",
    "installed",
]

_GUARD_SUMMARY = "resource guard: no unclosed sqlite connection, listening socket or http response"
_SHIM = "randreport/store/db.py"


@dataclass(frozen=True)
class OpenResource:
    """One handle that is still open when the process is about to exit."""

    kind: str
    created_at: str
    detail: str


class ResourceLeakError(RuntimeError):
    """Raised by :func:`check` when the process is about to exit with open handles."""

    def __init__(self, resources: Iterable[OpenResource]) -> None:
        self.resources = tuple(resources)
        super().__init__(f"{len(self.resources)} unclosed resource(s)")


# --- instrumentation ------------------------------------------------------- #

#: handles the guard has handed out and that it inspects before the process exits
_connections: list["TrackedConnection"] = []
_servers: list[tuple[socketserver.BaseServer, socket.socket]] = []
_sites: dict[int, str] = {}
_patch_factories = True
_installed = False


class TrackedConnection(sqlite3.Connection):
    """A connection that remembers whether ``close()`` was called.

    Reading the state back from the handle does not work: the per-thread guard
    makes a cross-thread probe fail for an *open* and a *closed* handle alike --
    that ambiguity hid the editorial desk's leaked handle for a whole acceptance
    round.  Remembering the close is exact and works across threads.

    The class deliberately defines no ``__del__``: a Python-level finaliser
    replaces the C finaliser of ``sqlite3.Connection``, and that C finaliser is
    what emits the ``unclosed database`` ResourceWarning.  The guard must add a
    failure, never take the warning trail away.  For the same reason the registry
    keeps the handle alive instead: a connection finalised mid-run cannot be
    inspected at exit, and holding it defers nothing but its own warning.
    """

    _guard_closed = False
    _guard_site = "<unknown>"

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._guard_closed = True


def _root() -> str:
    # randreport/leakguard.py -> the tree that owns the application
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _creation_site() -> str:
    """Who opened the handle: the innermost application frames of the caller.

    The generic ``store.db.connect`` shim and this module are skipped so the
    report names the code that owns the handle; stdlib frames are dropped, but
    they are used as a fallback (the wsgiref server opens its socket inside
    ``http/server.py``).
    """

    here = os.path.abspath(__file__)
    root = _root()
    app_frames: list[str] = []
    all_frames: list[str] = []
    for frame in traceback.extract_stack():
        if frame.filename == here or frame.filename.startswith("<"):
            continue
        name = os.path.abspath(frame.filename)
        try:
            rel = os.path.relpath(name, root)
        except ValueError:  # pragma: no cover - different drive (Windows only)
            rel = name
        if rel.startswith(".."):
            all_frames.append(f"{frame.filename}:{frame.lineno}")
            continue
        all_frames.append(f"{rel}:{frame.lineno}")
        if rel != _SHIM:
            app_frames.append(f"{rel}:{frame.lineno}")
    frames = app_frames or all_frames
    if not frames:
        return "<unknown>"
    return " <- ".join(reversed(frames[-3:]))


def _remember(obj: object) -> None:
    _sites[id(obj)] = _creation_site()


def _install_factories() -> None:
    """Instrument the factories that hand out handles.

    Connections come back as :class:`TrackedConnection` (a caller that brings its
    own ``factory`` keeps it); listening sockets are kept alive on purpose,
    because a listening socket the owner forgot to close is exactly what the exit
    check has to see.
    """

    global _patch_factories
    if not _patch_factories:
        return
    _patch_factories = False

    real_connect = sqlite3.connect

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        # ``factory`` is the sixth positional parameter of sqlite3.connect
        if "factory" not in kwargs and len(args) < 6:
            kwargs["factory"] = TrackedConnection
        conn = real_connect(*args, **kwargs)
        if isinstance(conn, TrackedConnection):
            conn._guard_site = _creation_site()
            _connections.append(conn)
        return conn

    sqlite3.connect = connect  # type: ignore[assignment]

    real_tcp_init = socketserver.TCPServer.__init__

    def tcp_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        real_tcp_init(self, *args, **kwargs)
        _remember(self.socket)
        _servers.append((self, self.socket))

    socketserver.TCPServer.__init__ = tcp_init  # type: ignore[assignment]


# --- inspection ------------------------------------------------------------ #

def _socket_open(sock: object) -> bool:
    try:
        return sock.fileno() != -1  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an unusable socket is not an open handle
        return False


def _response_open(response: object) -> bool:
    fp = getattr(response, "fp", None)
    if fp is None:
        return False
    try:
        return not fp.closed
    except Exception:  # noqa: BLE001
        return False


def _iter_responses() -> Iterable[object]:
    for obj in gc.get_objects():
        if isinstance(obj, http.client.HTTPResponse) or (
            type(obj).__module__ == "urllib.error" and hasattr(obj, "fp")
        ):
            yield obj


def open_resources() -> tuple[OpenResource, ...]:
    """Every handle that is provably still open right now."""

    found: list[OpenResource] = []
    for conn in list(_connections):
        if not conn._guard_closed:
            found.append(OpenResource("sqlite connection", conn._guard_site, repr(conn)))
    for server, listener in list(_servers):
        if _socket_open(listener):
            found.append(
                OpenResource(
                    "listening socket",
                    _sites.get(id(listener), _sites.get(id(server), "<unknown>")),
                    f"{type(server).__name__} {listener!r}",
                )
            )
    for response in _iter_responses():
        if _response_open(response):
            found.append(OpenResource("http response", "<untracked>", repr(response)))
    return tuple(found)


def check() -> None:
    """Raise :class:`ResourceLeakError` if anything is still open."""

    resources = open_resources()
    if resources:
        raise ResourceLeakError(resources)


def close_tracked_handles() -> int:
    """Close every handle the guard is holding, and return how many were closed.

    Only for a deliberate-leak fixture (the guard's own tests) that would
    otherwise make the suite's exit check fail; the exit check itself never
    closes anything, because that would hide the defect instead of reporting it.
    """

    closed = 0
    for conn in list(_connections):
        if not conn._guard_closed:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - an unusable handle
                continue
            closed += 1
    for server, listener in list(_servers):
        if _socket_open(listener):
            try:
                server.server_close()
            except Exception:  # noqa: BLE001 - pragma: no cover
                continue
            closed += 1
    return closed


def report(resources: Iterable[OpenResource], *, stream: TextIO | None = None) -> None:
    """Write a report that names the creation site of every open handle."""

    out = stream if stream is not None else sys.stderr
    items = tuple(resources)
    out.write(f"resource leak: {len(items)} handle(s) are still open before exit\n")
    counts: dict[tuple[str, str], int] = {}
    for item in items:
        key = (item.kind, item.created_at)
        counts[key] = counts.get(key, 0) + 1
    for (kind, site), count in counts.items():
        suffix = f"  ({count} handles)" if count > 1 else ""
        out.write(f"  {kind:<20} created at {site}{suffix}\n")
    out.write(
        "resource leak: close the handle where it is owned (context manager or try/finally);"
        " the leak is never silenced, only reported\n"
    )


# --- exit hook ------------------------------------------------------------- #

def _emit_warnings(resources: Iterable[OpenResource]) -> None:
    """Emit the warning Python would have emitted while finalising these handles.

    The exit check makes the status non-zero with ``os._exit``, which skips the
    interpreter's own finalisation -- and with it the ``ResourceWarning`` that
    CPython prints for a handle that was never closed.  The guard therefore
    emits the same warning itself, so the trail a reviewer looks for is still
    there; the ordinary filters decide whether it is printed, exactly as they do
    for CPython's own warning.
    """

    for item in resources:
        word = "database" if item.kind == "sqlite connection" else item.kind
        try:
            warnings.warn(
                f"unclosed {word} in <{item.detail.strip('<>')}> (pre-exit check)",
                ResourceWarning,
                stacklevel=2,
            )
        except Exception:  # noqa: BLE001 - a warning must never break the exit path
            pass


def _before_exit(*, verbose: bool, exit_code: int, stream: TextIO | None) -> None:
    out = stream if stream is not None else sys.stderr
    try:
        resources = open_resources()
    except Exception as exc:  # noqa: BLE001 - the check must never mask the real exit path
        out.write(f"resource guard: the check itself failed: {exc!r}\n")
        return
    if resources:
        report(resources, stream=out)
        _emit_warnings(resources)
        try:
            out.flush()
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        # sys.exit() inside an atexit callback is ignored by CPython (the
        # callback is reported as "Exception ignored"), so the status has to be
        # set explicitly to make the leak fail the process.
        os._exit(exit_code)
    if verbose:
        out.write(_GUARD_SUMMARY + "\n")


def installed() -> bool:
    """True once :func:`install` has registered the exit check."""

    return _installed


def install(*, verbose: bool = False, exit_code: int = 1, stream: TextIO | None = None) -> bool:
    """Instrument the factories and check the process before it exits.

    Returns ``True`` the first time it is called and ``False`` afterwards, so a
    caller can tell whether it is the one that installed the guard.  ``verbose``
    prints one confirmation line when the process is clean; a leak is always
    reported and always turns into ``exit_code``.
    """

    global _installed
    _install_factories()
    if _installed:
        return False
    _installed = True
    atexit.register(lambda: _before_exit(verbose=verbose, exit_code=exit_code, stream=stream))
    return True
