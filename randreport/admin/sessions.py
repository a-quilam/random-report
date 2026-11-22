"""Sessions, CSRF tokens and login throttling for the closed desk (section 11).

A session is a random cookie value whose **hash** is stored; the raw value is
never written to the database, a log or the audit trail. Every state-changing
request must carry the session's CSRF token, and a login that a member loses
(removal from the allowlist) loses its sessions immediately — the desk also
re-checks membership on every request, so a removal takes effect at the next
request even without the explicit revoke.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..store.db import transaction

COOKIE_NAME = "rd_admin_session"
CSRF_FIELD = "csrf"
SESSION_TTL = timedelta(hours=12)
MAX_LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    session_id: str          # the raw cookie value; only its hash is stored
    user_id: str
    csrf_token: str
    created_at: str
    expires_at: str
    display_name: str | None = None


def _row_to_session(raw_id: str, row: sqlite3.Row) -> Session:
    return Session(
        session_id=raw_id,
        user_id=str(row["telegram_user_id"]),
        csrf_token=str(row["csrf_token"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        display_name=row["display_name"],
    )


def create_session(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    display_name: str | None = None,
    now: datetime | None = None,
    ttl: timedelta = SESSION_TTL,
    rotated_from: str | None = None,
) -> Session:
    """Issue a fresh session (and a fresh CSRF token) for ``user_id``."""

    moment = now or _utcnow()
    raw_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = moment + ttl
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO admin_sessions (session_hash, telegram_user_id, display_name, created_at, expires_at, csrf_token, rotated_from)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _hash(raw_id),
                str(user_id),
                display_name,
                moment.isoformat(),
                expires.isoformat(),
                csrf,
                None if rotated_from is None else _hash(rotated_from),
            ),
        )
    return Session(
        session_id=raw_id,
        user_id=str(user_id),
        csrf_token=csrf,
        created_at=moment.isoformat(),
        expires_at=expires.isoformat(),
        display_name=display_name,
    )


def load_session(conn: sqlite3.Connection, raw_id: str | None, *, now: datetime | None = None) -> Session | None:
    """Return the live session for a cookie value, or ``None`` when it is not usable."""

    if not raw_id or not isinstance(raw_id, str):
        return None
    row = conn.execute("SELECT * FROM admin_sessions WHERE session_hash = ?", (_hash(raw_id),)).fetchone()
    if row is None or row["revoked_at"]:
        return None
    moment = now or _utcnow()
    try:
        expires = datetime.fromisoformat(str(row["expires_at"]))
    except ValueError:  # pragma: no cover - defensive
        return None
    if expires <= moment:
        return None
    return _row_to_session(raw_id, row)


def rotate(conn: sqlite3.Connection, raw_id: str, *, now: datetime | None = None, ttl: timedelta = SESSION_TTL) -> Session | None:
    """Rotate a session in place: the old value is revoked, a new one is issued."""

    current = load_session(conn, raw_id, now=now)
    if current is None:
        return None
    revoke(conn, raw_id, reason="rotated", now=now)
    return create_session(conn, user_id=current.user_id, display_name=current.display_name, now=now, ttl=ttl, rotated_from=raw_id)


def revoke(conn: sqlite3.Connection, raw_id: str, *, reason: str, now: datetime | None = None) -> bool:
    stamp = (now or _utcnow()).isoformat()
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE admin_sessions SET revoked_at = ?, revoked_reason = ? WHERE session_hash = ? AND revoked_at IS NULL",
            (stamp, reason, _hash(raw_id)),
        )
    return cursor.rowcount > 0


def revoke_all_for_user(conn: sqlite3.Connection, user_id: str, *, reason: str, now: datetime | None = None) -> int:
    stamp = (now or _utcnow()).isoformat()
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE admin_sessions SET revoked_at = ?, revoked_reason = ? WHERE telegram_user_id = ? AND revoked_at IS NULL",
            (stamp, reason, str(user_id)),
        )
    return int(cursor.rowcount)


def active_sessions(conn: sqlite3.Connection, user_id: str | None = None) -> tuple[dict[str, Any], ...]:
    if user_id is None:
        rows = conn.execute(
            "SELECT telegram_user_id, created_at, expires_at, rotated_from FROM admin_sessions WHERE revoked_at IS NULL ORDER BY created_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT telegram_user_id, created_at, expires_at, rotated_from FROM admin_sessions WHERE revoked_at IS NULL AND telegram_user_id = ? ORDER BY created_at",
            (str(user_id),),
        ).fetchall()
    return tuple(dict(row) for row in rows)


def csrf_matches(session: Session | None, token: str | None) -> bool:
    """Constant-time comparison of the session's CSRF token."""

    if session is None or not token:
        return False
    return hmac.compare_digest(session.csrf_token, token)


def cookie(session: Session, *, secure: bool = True, path: str = "/admin") -> tuple[str, str]:
    attrs = f"Path={path}; HttpOnly; SameSite=Lax"
    if secure:
        attrs += "; Secure"
    return ("Set-Cookie", f"{COOKIE_NAME}={session.session_id}; {attrs}")


def cleared_cookie(*, secure: bool = True, path: str = "/admin") -> tuple[str, str]:
    attrs = f"Path={path}; HttpOnly; SameSite=Lax; Max-Age=0"
    if secure:
        attrs += "; Secure"
    return ("Set-Cookie", f"{COOKIE_NAME}=; {attrs}")


def parse_cookies(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        name, _, value = chunk.strip().partition("=")
        if name:
            out[name] = value
    return out


# --------------------------------------------------------------------------- #
# login throttling
# --------------------------------------------------------------------------- #

def record_login(conn: sqlite3.Connection, *, subject: str | None, outcome: str, reason: str | None = None, now: datetime | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO admin_login_events (at, subject, outcome, reason) VALUES (?, ?, ?, ?)",
            ((now or _utcnow()).isoformat(), subject, outcome, reason),
        )


def recent_failures(conn: sqlite3.Connection, subject: str, *, now: datetime | None = None, window: timedelta = LOGIN_WINDOW) -> int:
    moment = now or _utcnow()
    since = (moment - window).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM admin_login_events WHERE subject = ? AND at >= ? AND outcome != 'ok'",
        (str(subject), since),
    ).fetchone()
    return int(row["n"] if row else 0)


def rate_limited(conn: sqlite3.Connection, subject: str, *, now: datetime | None = None, limit: int = MAX_LOGIN_ATTEMPTS, window: timedelta = LOGIN_WINDOW) -> bool:
    return recent_failures(conn, subject, now=now, window=window) >= limit


def prune_login_events(conn: sqlite3.Connection, *, now: datetime | None = None, keep: timedelta = timedelta(days=30)) -> int:
    """Retention for the throttling table; keeps the audit trail compact."""

    cutoff = ((now or _utcnow()) - keep).isoformat()
    with transaction(conn):
        cursor = conn.execute("DELETE FROM admin_login_events WHERE at < ?", (cutoff,))
    return int(cursor.rowcount)
