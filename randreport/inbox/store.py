"""Durable storage for the webhook inbox (``telegram_inbox``).

Intake and processing are deliberately separate:

* :func:`accept_post` writes one incoming update and returns only after the
  transaction committed, so the HTTP 200 the webhook sends means "it is on
  disk", not "it is in memory";
* the ``update_id`` is the idempotency key, exactly as Telegram retries it, so a
  replayed delivery cannot create a second row;
* :func:`pending_posts` collapses the pending rows per message (the newest
  version wins), and :func:`commit_posts` marks every row of those messages
  committed in one transaction — the archive write and the inbox marker never
  drift apart;
* at most :data:`MAX_BATCH` messages are committed per nightly pass; whatever is
  left stays pending and is visible in the counts instead of being dropped.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from ..store.db import transaction

INBOX_SOURCE = "telegram_posts"
KINDS = ("channel_post", "edited_channel_post")
MAX_BATCH = 200
#: Telegram's own per-message text limit; a longer body is a malformed update
MAX_TEXT_CHARS = 4096


class InboxError(ValueError):
    """An update that must not be written to the inbox."""


def _stamp(moment: datetime | None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat()


@dataclass(frozen=True)
class InboxPost:
    """One received public channel post, normalised for storage."""

    update_id: str
    kind: str
    channel_id: str
    message_id: str
    posted_at: datetime
    text: str
    edited_at: datetime | None = None
    media_group_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise InboxError(f"unsupported update kind {self.kind!r}")
        if not self.update_id or not self.message_id or not self.channel_id:
            raise InboxError("update_id, message_id and channel_id must be non-empty")
        if self.posted_at.tzinfo is None:
            raise InboxError("posted_at must be timezone aware")
        if self.edited_at is not None and self.edited_at.tzinfo is None:
            raise InboxError("edited_at must be timezone aware")
        if len(self.text) > MAX_TEXT_CHARS:
            raise InboxError(f"post text is longer than {MAX_TEXT_CHARS} characters")

    @property
    def content_hash(self) -> str:
        parts = (
            self.channel_id,
            self.message_id,
            self.text,
            self.posted_at.isoformat(),
            "" if self.edited_at is None else self.edited_at.isoformat(),
        )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]

    def to_raw_post(self):
        """The archive shape for this post (``public_message_archive``).

        The archive tracks its own content hash and revision; the inbox revision
        is a count of received versions and is not what the archive stores.
        """

        from ..connectors.telegram import RawPost

        return RawPost(
            message_id=self.message_id,
            channel_id=self.channel_id,
            posted_at=self.posted_at,
            text=self.text,
            edited_at=self.edited_at,
            revision=1,
            deleted=False,
            media_album_id=self.media_group_id,
        )


@dataclass(frozen=True)
class AcceptOutcome:
    """What the intake did: ``accepted`` (new), ``revised`` (newer version) or ``duplicate``."""

    status: str
    update_id: str
    message_id: str
    revision: int

    @property
    def stored(self) -> bool:
        """True when this call changed the inbox."""

        return self.status != "duplicate"


def _existing(conn: sqlite3.Connection, update_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM telegram_inbox WHERE update_id = ?", (update_id,)).fetchone()


def accept_post(conn: sqlite3.Connection, post: InboxPost) -> AcceptOutcome:
    """Write one incoming update durably; a replayed ``update_id`` is a no-op."""

    row = _existing(conn, post.update_id)
    if row is not None:
        # Telegram retries the same update_id until it sees a 2xx: this is the
        # exact replay the inbox has to absorb without a second row.
        return AcceptOutcome("duplicate", post.update_id, str(row["message_id"]), int(row["revision"]))

    latest = conn.execute(
        "SELECT update_id, revision, content_hash FROM telegram_inbox"
        " WHERE channel_id = ? AND message_id = ? ORDER BY revision DESC, received_at DESC, update_id DESC LIMIT 1",
        (post.channel_id, post.message_id),
    ).fetchone()
    same_version = conn.execute(
        "SELECT revision FROM telegram_inbox WHERE channel_id = ? AND message_id = ? AND content_hash = ? LIMIT 1",
        (post.channel_id, post.message_id, post.content_hash),
    ).fetchone()
    if same_version is not None:
        # the same version under a new update_id (a re-delivery): record the id
        # for future replay detection at the revision it belongs to, and do not
        # let it overwrite a newer version in the pending set
        revision = int(same_version["revision"])
        status = "duplicate"
    elif latest is None:
        revision = 1
        status = "accepted"
    else:
        revision = int(latest["revision"]) + 1
        status = "revised"
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO telegram_inbox (
                update_id, kind, channel_id, message_id, posted_at, edited_at, text,
                media_group_id, content_hash, revision, received_at, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                post.update_id,
                post.kind,
                post.channel_id,
                post.message_id,
                post.posted_at.isoformat(),
                None if post.edited_at is None else post.edited_at.isoformat(),
                post.text,
                post.media_group_id,
                post.content_hash,
                revision,
                post.received_at.isoformat(),
            ),
        )
    return AcceptOutcome(status, post.update_id, post.message_id, revision)


def _to_post(row: sqlite3.Row) -> InboxPost:
    return InboxPost(
        update_id=str(row["update_id"]),
        kind=str(row["kind"]),
        channel_id=str(row["channel_id"]),
        message_id=str(row["message_id"]),
        posted_at=datetime.fromisoformat(row["posted_at"]),
        text=str(row["text"]),
        edited_at=None if row["edited_at"] is None else datetime.fromisoformat(row["edited_at"]),
        media_group_id=None if row["media_group_id"] is None else str(row["media_group_id"]),
        received_at=datetime.fromisoformat(row["received_at"]),
    )


def pending_posts(conn: sqlite3.Connection, *, limit: int = MAX_BATCH) -> tuple[InboxPost, ...]:
    """The newest pending version of each message, oldest post first.

    Several pending rows can describe one message (an edit arrived before the
    nightly pass, or a re-delivery under a new ``update_id``).  The newest
    version — ordered by the intake revision, then by arrival — is what the
    archive gets; the other rows are committed with it so nothing is
    reprocessed forever.
    """

    rows = conn.execute(
        "SELECT * FROM telegram_inbox WHERE committed_at IS NULL"
        " ORDER BY revision, received_at, update_id"
    ).fetchall()
    newest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["channel_id"]), str(row["message_id"]))
        # the rows arrive in ascending version order, so the last one wins
        newest[key] = row
    ordered = sorted(newest.values(), key=lambda row: (str(row["posted_at"]), str(row["message_id"])))
    return tuple(_to_post(row) for row in ordered[: max(limit, 0)])


def commit_posts(conn: sqlite3.Connection, posts: Sequence[InboxPost], *, now: datetime | None = None) -> int:
    """Mark every pending row of these messages committed in one transaction."""

    if not posts:
        return 0
    stamp = _stamp(now)
    keys = {(post.channel_id, post.message_id) for post in posts}
    with transaction(conn):
        marked = 0
        for channel_id, message_id in sorted(keys):
            cursor = conn.execute(
                "UPDATE telegram_inbox SET committed_at = ? WHERE channel_id = ? AND message_id = ?"
                " AND committed_at IS NULL",
                (stamp, channel_id, message_id),
            )
            marked += cursor.rowcount
    return marked


def inbox_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Diagnostics for ``status``: what is pending, committed and known."""

    row = conn.execute(
        """
        SELECT
            COUNT(*)                                                        AS rows_total,
            SUM(CASE WHEN committed_at IS NULL THEN 1 ELSE 0 END)           AS pending_rows,
            SUM(CASE WHEN committed_at IS NOT NULL THEN 1 ELSE 0 END)       AS committed_rows,
            COUNT(DISTINCT channel_id || ':' || message_id)                 AS messages,
            COUNT(DISTINCT CASE WHEN committed_at IS NULL
                                THEN channel_id || ':' || message_id END)   AS pending_messages
        FROM telegram_inbox
        """
    ).fetchone()
    return {
        "rows_total": int(row["rows_total"] or 0),
        "pending_rows": int(row["pending_rows"] or 0),
        "committed_rows": int(row["committed_rows"] or 0),
        "messages": int(row["messages"] or 0),
        "pending_messages": int(row["pending_messages"] or 0),
    }
