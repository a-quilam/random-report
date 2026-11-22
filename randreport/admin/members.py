"""The allowlist of active commune members (docs/REQUIREMENTS.md section 11).

The desk is **deny by default**: without a row in ``admin_members`` nobody gets
in, and the public channel's subscribers never do. Every active member has the
same rights — there is no super-admin for ordinary work — and the last active
member cannot be removed, so the desk can never be locked out by accident.
Removing a member revokes that member's sessions immediately (and the desk also
re-checks membership on every request).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..store import repository as repo
from ..store.db import transaction
from . import sessions

_TELEGRAM_ID = re.compile(r"^[0-9]{1,20}$")


class MemberError(ValueError):
    """A membership change that must not be applied."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_telegram_id(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if not _TELEGRAM_ID.match(text):
        raise MemberError("a Telegram user id is digits only")
    return text


@dataclass(frozen=True)
class Member:
    telegram_user_id: str
    added_at: str
    revoked_at: str | None
    note: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def _member(row: sqlite3.Row) -> Member:
    return Member(
        telegram_user_id=str(row["telegram_user_id"]),
        added_at=str(row["added_at"]),
        revoked_at=row["revoked_at"],
        note=row["note"],
    )


def all_members(conn: sqlite3.Connection) -> tuple[Member, ...]:
    rows = conn.execute("SELECT * FROM admin_members ORDER BY added_at, telegram_user_id").fetchall()
    return tuple(_member(row) for row in rows)


def active_members(conn: sqlite3.Connection) -> tuple[Member, ...]:
    return tuple(member for member in all_members(conn) if member.active)


def is_member(conn: sqlite3.Connection, user_id: Any) -> bool:
    try:
        text = valid_telegram_id(user_id)
    except MemberError:
        return False
    row = conn.execute(
        "SELECT revoked_at FROM admin_members WHERE telegram_user_id = ?", (text,)
    ).fetchone()
    return row is not None and row["revoked_at"] is None


def member_state(conn: sqlite3.Connection, user_id: Any) -> str:
    try:
        text = valid_telegram_id(user_id)
    except MemberError:
        return "absent"
    row = conn.execute("SELECT revoked_at FROM admin_members WHERE telegram_user_id = ?", (text,)).fetchone()
    if row is None:
        return "absent"
    return "active" if row["revoked_at"] is None else "revoked"


def add_member(
    conn: sqlite3.Connection,
    user_id: Any,
    *,
    actor: str | None = None,
    note: str | None = None,
    now: str | None = None,
) -> Member:
    """Add or reinstate a member. The change is audited without secrets."""

    text = valid_telegram_id(user_id)
    stamp = now or _utcnow()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO admin_members (telegram_user_id, added_at, revoked_at, note) VALUES (?, ?, NULL, ?)
            ON CONFLICT (telegram_user_id) DO UPDATE SET revoked_at = NULL, note = COALESCE(excluded.note, admin_members.note)
            """,
            (text, stamp, note),
        )
    repo.audit(conn, action="admin.member.added", actor=actor, entity_type="admin_member", entity_key=text, detail={"note": note}, now=stamp)
    return Member(telegram_user_id=text, added_at=stamp, revoked_at=None, note=note)


def revoke_member(
    conn: sqlite3.Connection,
    user_id: Any,
    *,
    actor: str | None = None,
    reason: str = "removed",
    now: str | None = None,
) -> int:
    """Revoke a member and every live session of theirs. The last one is protected."""

    text = valid_telegram_id(user_id)
    current = active_members(conn)
    if not any(member.telegram_user_id == text for member in current):
        raise MemberError("this Telegram user is not an active member")
    if len(current) <= 1:
        raise MemberError("the last active member cannot be removed; add another member first")
    stamp = now or _utcnow()
    with transaction(conn):
        conn.execute(
            "UPDATE admin_members SET revoked_at = ? WHERE telegram_user_id = ? AND revoked_at IS NULL",
            (stamp, text),
        )
    revoked = sessions.revoke_all_for_user(conn, text, reason=reason, now=datetime.fromisoformat(stamp))
    repo.audit(
        conn,
        action="admin.member.revoked",
        actor=actor,
        entity_type="admin_member",
        entity_key=text,
        detail={"reason": reason, "sessions_revoked": revoked},
        now=stamp,
    )
    return revoked


def ensure_bootstrap(conn: sqlite3.Connection, user_id: Any, *, note: str = "bootstrap") -> bool:
    """Seed the first member from the agreed bootstrap id.

    Only a *virgin* table is seeded, so a desk whose members were later revoked
    cannot be re-opened by this path (the bootstrap id is not a back door).
    """

    if conn.execute("SELECT COUNT(*) AS n FROM admin_members").fetchone()["n"]:
        return False
    text = valid_telegram_id(user_id)
    add_member(conn, text, actor="bootstrap", note=note)
    repo.audit(conn, action="admin.member.bootstrap", actor="bootstrap", entity_type="admin_member", entity_key=text)
    return True


def change_membership(
    conn: sqlite3.Connection,
    *,
    actor: str,
    add: Any = None,
    remove: Any = None,
    now: str | None = None,
) -> dict[str, Any]:
    """One audited membership change, used by the desk UI and the CLI."""

    if add is not None and remove is not None:
        raise MemberError("change one member at a time")
    if add is not None:
        member = add_member(conn, add, actor=actor, now=now)
        return {"action": "added", "member": member.telegram_user_id, "sessions_revoked": 0}
    if remove is not None:
        revoked = revoke_member(conn, remove, actor=actor, now=now)
        return {"action": "revoked", "member": str(remove), "sessions_revoked": revoked}
    raise MemberError("nothing to change")
