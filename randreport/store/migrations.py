"""Versioned SQLite migrations for the data model of docs/REQUIREMENTS.md section 10.

Migrations are append-only tuples of SQL statements. ``apply_migrations`` is
idempotent: it records the applied versions and only runs the pending ones, each
inside its own transaction, so a fresh database and an existing database converge
to the same schema (O01 "migration on a clean database").
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import transaction


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


_INITIAL = (
    # --- run bookkeeping and per-source reliability (section 9) ---
    """
    CREATE TABLE source_runs (
        run_id          TEXT NOT NULL,
        source          TEXT NOT NULL,
        logical_date    TEXT NOT NULL,          -- idempotency key: MSK calendar day
        cutoff          TEXT NOT NULL,          -- ISO instant of the run cutoff
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        status          TEXT NOT NULL,          -- running|ok|partial|failed
        last_attempt_at TEXT NOT NULL,
        last_success_at TEXT,
        data_as_of      TEXT,
        coverage        TEXT,                   -- complete|partial|unknown
        error_count     INTEGER NOT NULL DEFAULT 0,
        new_rows        INTEGER NOT NULL DEFAULT 0,
        changed_rows    INTEGER NOT NULL DEFAULT 0,
        deleted_rows    INTEGER NOT NULL DEFAULT 0,
        error           TEXT,                   -- message only, never a payload or token
        PRIMARY KEY (run_id),
        UNIQUE (source, logical_date)
    )
    """,
    """
    CREATE TABLE source_cursors (
        source      TEXT PRIMARY KEY,
        cursor      TEXT NOT NULL,
        revision    INTEGER NOT NULL DEFAULT 1,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE source_coverage (
        source       TEXT NOT NULL,
        period_grain TEXT NOT NULL,             -- month|year|range|point
        period_from  TEXT NOT NULL,
        period_to    TEXT NOT NULL,
        coverage     TEXT NOT NULL,             -- complete|partial|unknown
        data_as_of   TEXT,
        provenance   TEXT,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (source, period_grain, period_from, period_to)
    )
    """,
    """
    CREATE TABLE last_good_snapshots (
        source      TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL,
        payload     TEXT NOT NULL,              -- JSON of the last validated source payload
        data_as_of  TEXT NOT NULL,
        coverage    TEXT NOT NULL,
        stored_at   TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES source_runs(run_id)
    )
    """,
    # --- private finance facts (section 6, 10) ---
    """
    CREATE TABLE finance_accounts (
        account_id   TEXT PRIMARY KEY,
        title        TEXT,
        currency     TEXT NOT NULL DEFAULT 'RUB',
        account_type TEXT,
        scope        TEXT NOT NULL DEFAULT 'unknown',   -- public|private|unknown
        updated_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE finance_categories (
        category_id TEXT PRIMARY KEY,
        title       TEXT,
        raw_title   TEXT,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE category_mapping_versions (
        version    TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        payload    TEXT NOT NULL                -- JSON of the ScopePolicy mapping
    )
    """,
    """
    CREATE TABLE finance_transactions (
        transaction_id  TEXT PRIMARY KEY,
        occurred_on     TEXT NOT NULL,          -- fact date in Europe/Moscow
        amount_kopecks  INTEGER NOT NULL,
        source_account  TEXT,
        target_account  TEXT,
        category_id     TEXT,
        currency        TEXT NOT NULL DEFAULT 'RUB',
        pair_id         TEXT,
        pair_kind       TEXT,
        reimbursement_of TEXT,
        is_planned      INTEGER NOT NULL DEFAULT 0,
        deleted         INTEGER NOT NULL DEFAULT 0,
        source_revision INTEGER NOT NULL DEFAULT 1,
        notes           TEXT NOT NULL DEFAULT '[]',
        content_hash    TEXT NOT NULL,
        ingested_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_finance_transactions_day ON finance_transactions(occurred_on)",
    """
    CREATE TABLE finance_allocations_or_classification (
        transaction_id TEXT NOT NULL,
        economic_class TEXT NOT NULL,
        mapping_version TEXT NOT NULL,
        note           TEXT,
        PRIMARY KEY (transaction_id, mapping_version),
        FOREIGN KEY (transaction_id) REFERENCES finance_transactions(transaction_id)
    )
    """,
    """
    CREATE TABLE account_balance_snapshots (
        account_id      TEXT NOT NULL,
        as_of           TEXT NOT NULL,
        balance_kopecks INTEGER NOT NULL,
        currency        TEXT NOT NULL DEFAULT 'RUB',
        is_liquid       INTEGER NOT NULL DEFAULT 1,
        confirmed       INTEGER NOT NULL DEFAULT 1,
        scope           TEXT NOT NULL DEFAULT 'public',
        source          TEXT NOT NULL,
        ingested_at     TEXT NOT NULL,
        PRIMARY KEY (account_id, as_of)
    )
    """,
    # --- private calendar facts (section 7, 10) ---
    """
    CREATE TABLE calendar_occurrences (
        calendar_id     TEXT NOT NULL,
        occurrence_id   TEXT NOT NULL,
        series_id       TEXT,
        start_utc       TEXT NOT NULL,
        end_utc         TEXT,
        all_day         INTEGER NOT NULL DEFAULT 0,
        subcalendar_ids TEXT NOT NULL DEFAULT '[]',
        title           TEXT,
        raw_type        TEXT,
        revision        INTEGER NOT NULL DEFAULT 1,
        deleted         INTEGER NOT NULL DEFAULT 0,
        source_present  INTEGER NOT NULL DEFAULT 1,
        local_archive   INTEGER NOT NULL DEFAULT 0,
        content_hash    TEXT NOT NULL,
        ingested_at     TEXT NOT NULL,
        PRIMARY KEY (calendar_id, occurrence_id)
    )
    """,
    "CREATE INDEX idx_calendar_occurrences_start ON calendar_occurrences(start_utc)",
    """
    CREATE TABLE event_type_mapping (
        version    TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        payload    TEXT NOT NULL
    )
    """,
    # --- channel and public post archive (section 8, 10) ---
    """
    CREATE TABLE channel_snapshots (
        on_date     TEXT PRIMARY KEY,           -- MSK snapshot day
        subscribers INTEGER NOT NULL,
        source      TEXT NOT NULL,
        revision    INTEGER NOT NULL DEFAULT 1,
        received_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE public_message_archive (
        message_id     TEXT PRIMARY KEY,
        channel_id     TEXT NOT NULL,
        posted_at      TEXT NOT NULL,
        edited_at      TEXT,
        text           TEXT NOT NULL,
        source_deleted INTEGER NOT NULL DEFAULT 0,
        revision       INTEGER NOT NULL DEFAULT 1,
        received_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE news_sources (
        source_id  TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE news_items (
        news_id            TEXT PRIMARY KEY,
        classification     TEXT NOT NULL,
        category           TEXT,
        title              TEXT,
        summary            TEXT,
        published_on       TEXT,
        event_date         TEXT,
        confidence         TEXT,
        source_message_ids TEXT NOT NULL DEFAULT '[]',
        public_only        INTEGER NOT NULL DEFAULT 1,
        machine_processed  INTEGER NOT NULL DEFAULT 1,
        verified           INTEGER NOT NULL DEFAULT 0,
        manual_state       TEXT NOT NULL DEFAULT 'active',   -- active|hidden|edited
        origin             TEXT NOT NULL,                      -- llm|manual
        prompt_version     TEXT,
        model              TEXT,
        content_hash       TEXT,
        source_deleted     INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        UNIQUE (news_id)
    )
    """,
    """
    CREATE TABLE donor_public_aliases (
        alias_id     TEXT PRIMARY KEY,
        public_label TEXT NOT NULL,
        allowed      INTEGER NOT NULL DEFAULT 1,
        note         TEXT
    )
    """,
    # --- history and provenance (section 12, 10) ---
    """
    CREATE TABLE historical_observations (
        observation_id TEXT PRIMARY KEY,
        metric         TEXT NOT NULL,
        period_from    TEXT NOT NULL,
        period_to      TEXT NOT NULL,
        grain          TEXT NOT NULL,           -- year|month|range|point
        value          TEXT,                    -- nullable integer/decimal as text
        unit           TEXT NOT NULL,
        source_id      TEXT NOT NULL,
        provenance     TEXT NOT NULL,           -- published|snapshot|derived|draft
        coverage       TEXT NOT NULL,
        confidence     TEXT,
        status         TEXT,
        conflict_group TEXT,
        notes          TEXT,
        ingested_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE historical_conflicts (
        conflict_id    TEXT PRIMARY KEY,
        conflict_group TEXT NOT NULL,
        metric         TEXT,
        period_from    TEXT,
        period_to      TEXT,
        published_value TEXT,
        computed_value  TEXT,
        difference      TEXT,
        status          TEXT NOT NULL DEFAULT 'open',
        notes           TEXT
    )
    """,
    # --- verification, issues, audit, reports, releases, admins (section 4, 10) ---
    """
    CREATE TABLE checks (
        check_id     TEXT PRIMARY KEY,
        entity_type  TEXT NOT NULL,
        entity_key   TEXT NOT NULL,
        revision_hash TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        state        TEXT NOT NULL,             -- operational|verified|conflict|no_data|stale
        checked_at   TEXT NOT NULL,
        note         TEXT
    )
    """,
    """
    CREATE TABLE issues (
        issue_id   TEXT PRIMARY KEY,
        code       TEXT NOT NULL,
        severity   TEXT NOT NULL,
        entity_type TEXT,
        entity_key  TEXT,
        payload    TEXT,                        -- JSON, private detail only
        status     TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE audit_log (
        audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        at          TEXT NOT NULL,
        actor       TEXT,
        action      TEXT NOT NULL,
        entity_type TEXT,
        entity_key  TEXT,
        detail      TEXT                        -- JSON, no secrets, no raw payloads
    )
    """,
    """
    CREATE TABLE report_snapshots (
        report_id     TEXT PRIMARY KEY,
        period_label  TEXT NOT NULL,
        version       INTEGER NOT NULL,
        published_at  TEXT NOT NULL,
        payload       TEXT NOT NULL,
        original_text TEXT,
        original_url  TEXT,
        UNIQUE (period_label, version)
    )
    """,
    """
    CREATE TABLE public_releases (
        release_id     TEXT PRIMARY KEY,
        built_at       TEXT NOT NULL,
        as_of          TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        sections       TEXT NOT NULL DEFAULT '[]',
        payload        TEXT NOT NULL,           -- leak-checked public JSON
        is_current     INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_public_releases_current ON public_releases(is_current)",
    """
    CREATE TABLE admin_members (
        telegram_user_id TEXT PRIMARY KEY,
        added_at         TEXT NOT NULL,
        revoked_at       TEXT,
        note             TEXT
    )
    """,
)

_MIGRATION_2 = (
    # LLM cache keyed by hash(input + prompt_version + model) and a daily budget
    """
    CREATE TABLE extraction_cache (
        cache_key      TEXT PRIMARY KEY,
        prompt_version TEXT NOT NULL,
        model          TEXT NOT NULL,
        response       TEXT NOT NULL,
        created_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE llm_budget_ledger (
        logical_date TEXT PRIMARY KEY,
        calls        INTEGER NOT NULL DEFAULT 0,
        tokens       INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE deferred_jobs (
        job_id     TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        payload    TEXT NOT NULL,
        reason     TEXT,
        created_at TEXT NOT NULL
    )
    """,
)

_MIGRATION_3 = (
    # --- the closed editorial desk: sessions, news revisions, drafts (section 4, 11) ---
    """
    CREATE TABLE admin_sessions (
        session_hash     TEXT PRIMARY KEY,      -- sha256 of the cookie value, never the value
        telegram_user_id TEXT NOT NULL,
        display_name     TEXT,
        created_at       TEXT NOT NULL,
        expires_at       TEXT NOT NULL,
        revoked_at       TEXT,
        revoked_reason   TEXT,
        csrf_token       TEXT NOT NULL,
        rotated_from     TEXT,
        FOREIGN KEY (telegram_user_id) REFERENCES admin_members(telegram_user_id)
    )
    """,
    "CREATE INDEX idx_admin_sessions_user ON admin_sessions(telegram_user_id)",
    """
    CREATE TABLE admin_login_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at       TEXT NOT NULL,
        subject  TEXT,                          -- Telegram user id; never a token or a name
        outcome  TEXT NOT NULL,                 -- ok|denied|bad_signature|rate_limited|expired
        reason   TEXT
    )
    """,
    "CREATE INDEX idx_admin_login_events_subject ON admin_login_events(subject, at)",
    """
    CREATE TABLE news_revisions (
        news_id            TEXT NOT NULL,
        revision           INTEGER NOT NULL,
        origin             TEXT NOT NULL,       -- llm|manual
        classification     TEXT NOT NULL,
        category           TEXT,
        title              TEXT,
        summary            TEXT,
        source_message_ids TEXT NOT NULL DEFAULT '[]',
        actor              TEXT,
        note               TEXT,
        created_at         TEXT NOT NULL,
        PRIMARY KEY (news_id, revision),
        FOREIGN KEY (news_id) REFERENCES news_items(news_id)
    )
    """,
    """
    CREATE TABLE news_merges (
        merge_id     TEXT PRIMARY KEY,
        target_news_id TEXT NOT NULL,
        source_news_id TEXT NOT NULL,
        actor        TEXT,
        reason       TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        UNIQUE (target_news_id, source_news_id)
    )
    """,
    """
    CREATE TABLE post_drafts (
        period_label   TEXT PRIMARY KEY,
        status         TEXT NOT NULL DEFAULT 'draft',   -- draft|ready|closed
        comment        TEXT NOT NULL DEFAULT '',
        markdown       TEXT NOT NULL DEFAULT '',
        frozen_version INTEGER,
        updated_by     TEXT,
        updated_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE finance_annotations (
        transaction_id TEXT PRIMARY KEY,
        note           TEXT NOT NULL,
        actor          TEXT,
        updated_at     TEXT NOT NULL,
        FOREIGN KEY (transaction_id) REFERENCES finance_transactions(transaction_id)
    )
    """,
    """
    CREATE TABLE check_invalidations (
        invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at           TEXT NOT NULL,
        entity_type  TEXT NOT NULL,
        entity_key   TEXT,
        cause        TEXT NOT NULL,
        rules_version TEXT,
        affected     INTEGER NOT NULL DEFAULT 0,
        detail       TEXT
    )
    """,
)

# Migration 4: the source revision of an occurrence is an opaque token, not a
# number. ``version`` arrives from Teamup as a hex string; an INTEGER column has
# NUMERIC affinity and would silently rewrite a numeric-looking token (``0007``
# becomes ``7``, a 20-digit token becomes a float), so the column is rebuilt as
# TEXT and the existing tokens are kept as their text form.
_MIGRATION_4: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_calendar_occurrences_start",
    "ALTER TABLE calendar_occurrences RENAME TO calendar_occurrences_integer_revision",
    """
    CREATE TABLE calendar_occurrences (
        calendar_id     TEXT NOT NULL,
        occurrence_id   TEXT NOT NULL,
        series_id       TEXT,
        start_utc       TEXT NOT NULL,
        end_utc         TEXT,
        all_day         INTEGER NOT NULL DEFAULT 0,
        subcalendar_ids TEXT NOT NULL DEFAULT '[]',
        title           TEXT,
        raw_type        TEXT,
        revision        TEXT NOT NULL DEFAULT '',
        deleted         INTEGER NOT NULL DEFAULT 0,
        source_present  INTEGER NOT NULL DEFAULT 1,
        local_archive   INTEGER NOT NULL DEFAULT 0,
        content_hash    TEXT NOT NULL,
        ingested_at     TEXT NOT NULL,
        PRIMARY KEY (calendar_id, occurrence_id)
    )
    """,
    """
    INSERT INTO calendar_occurrences (
        calendar_id, occurrence_id, series_id, start_utc, end_utc, all_day,
        subcalendar_ids, title, raw_type, revision, deleted, source_present,
        local_archive, content_hash, ingested_at
    )
    SELECT
        calendar_id, occurrence_id, series_id, start_utc, end_utc, all_day,
        subcalendar_ids, title, raw_type, CAST(revision AS TEXT), deleted,
        source_present, local_archive, content_hash, ingested_at
      FROM calendar_occurrences_integer_revision
    """,
    "DROP TABLE calendar_occurrences_integer_revision",
    "CREATE INDEX idx_calendar_occurrences_start ON calendar_occurrences(start_utc)",
)

# --- migration 5: the passive webhook inbox ------------------------------- #
#
# ``docs/REQUIREMENTS.md`` section 8 allows a webhook as an *event intake*: the
# process writes each incoming ``channel_post``/``edited_channel_post`` durably
# into a private inbox and does all processing in the 03:00 cycle.  The inbox is
# the reason a single missed delivery never loses a post silently: what was
# received is on disk with the Telegram ``update_id`` as an idempotency key, and
# what was *not* received stays visible as a gap rather than being reported as
# "no news".
_MIGRATION_5 = (
    """
    CREATE TABLE telegram_inbox (
        update_id      TEXT PRIMARY KEY,          -- Telegram update_id: the dedup key
        kind           TEXT NOT NULL,             -- channel_post|edited_channel_post
        channel_id     TEXT NOT NULL,
        message_id     TEXT NOT NULL,
        posted_at      TEXT NOT NULL,
        edited_at      TEXT,
        text           TEXT NOT NULL,
        media_group_id TEXT,
        content_hash   TEXT NOT NULL,
        revision       INTEGER NOT NULL DEFAULT 1,
        received_at    TEXT NOT NULL,
        committed_at   TEXT
    )
    """,
    "CREATE INDEX idx_telegram_inbox_pending ON telegram_inbox(committed_at, received_at)",
    # one live row per (channel, message): an edit replaces the pending revision
    # instead of queueing a second copy of the same post
    "CREATE INDEX idx_telegram_inbox_message ON telegram_inbox(channel_id, message_id)",
    """
    CREATE TABLE news_imports (
        import_id    TEXT PRIMARY KEY,
        channel_id   TEXT NOT NULL,
        source_label TEXT NOT NULL,
        received     INTEGER NOT NULL DEFAULT 0,
        imported     INTEGER NOT NULL DEFAULT 0,
        unchanged    INTEGER NOT NULL DEFAULT 0,
        rejected     INTEGER NOT NULL DEFAULT 0,
        detail       TEXT,
        created_at   TEXT NOT NULL
    )
    """,
)

# --- migration 6: the durable getUpdates offset ---------------------------- #
# A webhook needs a public TLS endpoint; without one the same intake runs as a
# short getUpdates poll (``randreport/inbox/polling.py``).  The offset is kept
# here, in the private store, and it is advanced only *after* the inbox rows of
# the same poll are committed, so a crash between the two re-reads the same
# updates and the ``update_id`` key deduplicates them — nothing is lost.
_MIGRATION_6 = (
    """
    CREATE TABLE telegram_poll_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

# --- migration 7: reversible per-operation report exclusions ---------------- #
# A category that only *partly* belongs to the basement cannot be excluded as a
# whole; the editorial desk excludes a single operation instead.  The decision
# lives here, reversibly: the row is kept and merely marked restored, the source
# transaction in ``finance_transactions`` is never touched, and every change is
# audited.  A report then drops exactly this operation and keeps its immutable
# original (docs/ALLOCATION.md section 4).
_MIGRATION_7 = (
    """
    CREATE TABLE finance_report_exclusions (
        transaction_id TEXT PRIMARY KEY,
        excluded_at    TEXT NOT NULL,
        actor          TEXT,
        reason         TEXT NOT NULL DEFAULT '',
        restored_at    TEXT,
        restored_by    TEXT
    )
    """,
    "CREATE INDEX idx_report_exclusions_active ON finance_report_exclusions(restored_at)",
)

# --- migration 8: forwarding counters (MTProto) --------------------------- #
#
# The Bot API never exposes forward counters; a bot-logged MTProto client
# (Telethon 1.x) reads ``forwards``/``views`` per message id.  The ids come from
# ``public_message_archive`` — the archive is extended with the media-group id it
# already receives from the inbox but never stored — and the counters live in
# their own 1:1 table so the daily re-measurement never rewrites archive rows.
# ``forwards = NULL`` is "never measured"; a measured zero stays a zero.
_MIGRATION_8 = (
    "ALTER TABLE public_message_archive ADD COLUMN media_album_id TEXT",
    """
    CREATE TABLE post_forward_stats (
        message_id TEXT PRIMARY KEY,
        forwards   INTEGER,               -- NULL = unknown, never faked to 0
        views      INTEGER,               -- NULL = unknown
        fetched_at TEXT NOT NULL,         -- last successful measurement (UTC ISO)
        channel_id TEXT NOT NULL
    )
    """,
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial public-contract schema", _INITIAL),
    Migration(2, "llm cache, budget ledger and deferred jobs", _MIGRATION_2),
    Migration(3, "admin sessions, news revisions, drafts and annotations", _MIGRATION_3),
    Migration(4, "an occurrence revision is an opaque text token", _MIGRATION_4),
    Migration(5, "passive Telegram webhook inbox for public channel posts", _MIGRATION_5),
    Migration(6, "durable getUpdates offset for the domain-less intake", _MIGRATION_6),
    Migration(7, "reversible per-operation report exclusions", _MIGRATION_7),
    Migration(8, "Telegram forwarding counters measured over MTProto", _MIGRATION_8),
)

# Tables required by docs/REQUIREMENTS.md section 10.
REQUIRED_TABLES = frozenset(
    {
        "source_runs",
        "source_cursors",
        "source_coverage",
        "finance_accounts",
        "finance_categories",
        "finance_transactions",
        "finance_allocations_or_classification",
        "account_balance_snapshots",
        "category_mapping_versions",
        "calendar_occurrences",
        "event_type_mapping",
        "channel_snapshots",
        "public_message_archive",
        "news_items",
        "news_sources",
        "donor_public_aliases",
        "historical_observations",
        "historical_conflicts",
        "checks",
        "issues",
        "audit_log",
        "report_snapshots",
        "public_releases",
        "admin_members",
    }
)


def apply_migrations(conn: sqlite3.Connection, *, now: str | None = None) -> tuple[int, ...]:
    """Apply pending migrations; return the versions applied in this call."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        with transaction(conn):
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, now or _utcnow()),
            )
        newly.append(migration.version)
    return tuple(newly)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    return int(row["version"]) if row and row["version"] is not None else 0


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def missing_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Contract check: every table of section 10 must exist."""

    return tuple(sorted(REQUIRED_TABLES - _table_names(conn)))
