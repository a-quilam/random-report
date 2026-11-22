"""SQLite connection helpers (WAL, foreign keys, explicit transactions)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MEMORY = ":memory:"


def connect(path: str | Path = MEMORY, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with foreign keys on and WAL for on-disk databases.

    ``check_same_thread=False`` is for the long-lived WSGI applications: a wsgiref
    server may hand a request to a worker thread while the process shuts the
    connection down from the main thread, and a per-thread check would make that
    ``close()`` raise and leak the handle.
    """

    target = str(path)
    conn = sqlite3.connect(target, isolation_level=None, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != MEMORY:
        # WAL keeps readers working during the nightly write cycle
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one explicit transaction.

    Facts and cursors are written together, so a crash can never leave a cursor
    ahead of the data it describes (docs/REQUIREMENTS.md section 6.1). Nested use
    joins the outer transaction instead of opening a second one.
    """

    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
