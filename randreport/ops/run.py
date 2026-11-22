"""The nightly run: lease, per-source reliability, last-good and atomic publish.

Order of operations (docs/REQUIREMENTS.md section 9): fix the cutoff and run_id,
pull each source, validate and deduplicate, write facts and cursors in one
transaction, compute the aggregates, build a safe revision, publish atomically.
A failure of one source never zeroes another, and a new payload that fails a
safety check is not published — the previous safe release keeps serving.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..connectors.teamup import TeamupClient
from ..connectors.telegram import ChannelSubscriberPoint, RawPost, TelegramClient
from ..connectors.zenmoney import ZenmoneyClient
from ..domain.finance import AccountBalance
from ..domain.timeframes import MSK, msk_day
from ..news.extract import NewsExtractor
from ..store import repository as repo
from .pipeline import PublicRelease, ReleaseBuilder
from .publish import publish_release
from .schedule import logical_date_for, utc_now
from .status import SourceStatus, overall_freshness, source_status


class SourceError(RuntimeError):
    """A source that could not be read within the retry budget."""


class RunInProgress(RuntimeError):
    """Another nightly run holds the lease."""


@dataclass(frozen=True)
class SourceOutcome:
    source: str
    status: str = "ok"  # ok|partial|failed
    counts: repo.UpsertCounts = field(default_factory=repo.UpsertCounts)
    cursor: str | None = None
    data_as_of: str | None = None
    coverage: str = "complete"
    payload: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


class SourceAdapter(Protocol):
    name: str

    def sync(
        self,
        *,
        conn: sqlite3.Connection,
        cutoff: datetime,
        logical_date: str,
        cursor: str | None,
    ) -> SourceOutcome:  # pragma: no cover - protocol
        ...


class RunLease:
    """A single run lease, so two concurrent cycles cannot interleave."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RunInProgress(f"another run holds the lease at {self.path}") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RunLease":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@dataclass(frozen=True)
class RunReport:
    run_id: str
    logical_date: str
    cutoff: datetime
    sources: Mapping[str, SourceOutcome]
    statuses: Mapping[str, SourceStatus]
    release_id: str | None
    published: bool
    freshness: str
    completeness: str
    issues: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Source adapters
# --------------------------------------------------------------------------- #

class ZenmoneyAdapter:
    """Read-only Zenmoney sync, cursor advanced only after the facts commit."""

    name = "zenmoney"

    def __init__(
        self,
        client: ZenmoneyClient,
        *,
        balance_reader: Callable[[Any], Sequence[AccountBalance]] | None = None,
    ) -> None:
        self._client = client
        self._balance_reader = balance_reader

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        server_timestamp = int(cursor) if cursor else 0
        # the diff for the previous serverTimestamp; a lost cursor means a full resync
        diff = self._client.fetch_diff(server_timestamp=server_timestamp, now=cutoff)
        stamp = cutoff.isoformat()
        counts = repo.upsert_movements(conn, diff.movements, now=stamp)
        repo.mark_movements_deleted(conn, diff.deleted_ids, now=stamp)
        if diff.deleted_ids:
            counts = repo.UpsertCounts(
                new=counts.new,
                changed=counts.changed,
                deleted=counts.deleted + len(diff.deleted_ids),
                unchanged=counts.unchanged,
            )
        if self._balance_reader is not None:
            balances = tuple(self._balance_reader(diff))
            if balances:
                repo.upsert_balance_snapshots(conn, balances, source=self.name, now=stamp)
        total_rows = conn.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"]
        coverage = "complete" if not diff.currency_unknown else "partial"
        return SourceOutcome(
            source=self.name,
            status="ok",
            counts=counts,
            cursor=str(diff.server_timestamp),
            data_as_of=stamp,
            coverage=coverage,
            payload={
                "server_timestamp": diff.server_timestamp,
                "rows_read": len(diff.rows),
                "skipped_zero": diff.skipped_zero_amount,
                "deleted": len(diff.deleted_ids),
                "rows_stored": int(total_rows),
                "currency_unknown": list(diff.currency_unknown),
            },
        )


class TeamupAdapter:
    """Read-only Teamup sync with a wide window and a local retention archive."""

    name = "teamup"

    def __init__(self, client: TeamupClient, *, window_days: int = 400) -> None:
        self._client = client
        self._window_days = window_days

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        start, end = self._client.default_window(cutoff, depth_days=self._window_days)
        modified_since = datetime.fromisoformat(cursor) if cursor else None
        window = self._client.fetch_window(start_date=start, end_date=end, modified_since=modified_since)
        stamp = cutoff.isoformat()
        counting = repo.upsert_occurrences(conn, window.occurrences, now=stamp)
        deleted = repo.mark_occurrences_deleted(
            conn, calendar_id=self._client.calendar_id, ids=window.deleted_ids, now=stamp
        )
        archived = repo.archive_missing_occurrences(
            conn,
            calendar_id=self._client.calendar_id,
            present_ids=tuple(occ.occurrence_id for occ in window.occurrences),
            now=stamp,
        )
        return SourceOutcome(
            source=self.name,
            status="ok",
            counts=repo.UpsertCounts(
                new=counting.new, changed=counting.changed, deleted=counting.deleted + deleted, unchanged=counting.unchanged
            ),
            cursor=stamp,
            data_as_of=stamp,
            coverage="complete",
            payload={
                "events": len(window.occurrences),
                "deleted_from_source": len(window.deleted_ids),
                "marked_deleted": deleted,
                "archived_by_retention": archived,
                "pages": window.counts.pages,
            },
        )


class TelegramAdapter:
    """Daily subscriber snapshot plus a read-only post archive pull."""

    name = "telegram"

    def __init__(self, client: TelegramClient, *, chat_id: str, channel_id: str, batch: int = 100) -> None:
        self._client = client
        self._chat_id = chat_id
        self._channel_id = channel_id
        self._batch = batch

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        stamp = cutoff.isoformat()
        errors: list[str] = []
        snapshot_result = "error"
        subscribers: int | None = None
        try:
            point: ChannelSubscriberPoint = self._client.snapshot_subscribers(
                chat_id=self._chat_id, on_date=msk_day(cutoff), now=cutoff
            )
            subscribers = point.subscribers
            snapshot_result = repo.record_channel_snapshot(
                conn,
                on_date=point.on.isoformat(),
                subscribers=point.subscribers,
                source=self.name,
                received_at=point.received_at,
            )
        except Exception as exc:  # noqa: BLE001 - an error is a gap, never a zero
            errors.append(f"snapshot: {exc}")
            repo.record_issue(
                conn,
                issue_id=f"telegram_snapshot_missing:{logical_date}",
                code="telegram_snapshot_missing",
                severity="warning",
                entity_type="channel",
                entity_key=self._chat_id,
                payload={"reason": str(exc)},
                now=stamp,
            )

        posts: tuple[RawPost, ...] = ()
        try:
            posts = self._client.pull_posts(channel_id=self._channel_id, since_id=cursor, limit=self._batch)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"history: {exc}")

        counts = repo.upsert_posts(conn, posts, now=stamp) if posts else repo.UpsertCounts()
        next_cursor = posts[-1].message_id if posts else cursor
        status = "ok" if not errors else "partial"
        coverage = "complete" if not errors else "partial"
        return SourceOutcome(
            source=self.name,
            status=status,
            counts=counts,
            cursor=next_cursor,
            data_as_of=None if errors and not posts else stamp,
            coverage=coverage,
            payload={
                "snapshot": snapshot_result,
                "subscribers": subscribers,
                "posts_read": len(posts),
                "errors": errors,
            },
            error="; ".join(errors) or None,
        )


class NewsAdapter:
    """One daily LLM pass over new/changed public posts."""

    name = "news"

    def __init__(self, extractor: NewsExtractor, *, batch_size: int = 200) -> None:
        self._extractor = extractor
        self._batch_size = batch_size

    def _pending(self, conn: sqlite3.Connection, cursor: str | None) -> tuple[RawPost, ...]:
        posts = repo.archived_posts(conn)
        if cursor is None:
            return posts[: self._batch_size]
        wanted: list[RawPost] = []
        for post in posts:
            if post.message_id.isdigit() and cursor.isdigit():
                if int(post.message_id) > int(cursor):
                    wanted.append(post)
                    continue
            elif post.message_id > cursor:
                wanted.append(post)
                continue
            if post.edited_at is not None:
                wanted.append(post)
        return tuple(wanted[: self._batch_size])

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        pending = self._pending(conn, cursor)
        outcome = self._extractor.run(pending, logical_date=logical_date)
        # a deleted source post is removed from the public chronicle, history kept
        archived = {post.message_id: post for post in repo.archived_posts(conn, include_deleted=True)}
        for row in repo.public_news_items(conn):
            ids = json.loads(row["source_message_ids"])
            if ids and all(archived.get(mid) is not None and archived[mid].deleted for mid in ids):
                repo.mark_news_source_deleted(conn, news_id=row["news_id"], now=cutoff.isoformat())
        next_cursor = cursor
        if pending:
            numeric = [post.message_id for post in pending if post.message_id.isdigit()]
            if numeric:
                next_cursor = max(numeric, key=int)
            else:
                next_cursor = max(post.message_id for post in pending)
        coverage = "complete" if not (outcome.degraded or outcome.deferred) else "partial"
        return SourceOutcome(
            source=self.name,
            status="ok",
            cursor=next_cursor,
            data_as_of=cutoff.isoformat(),
            coverage=coverage,
            payload={
                "published": outcome.published,
                "announcements": outcome.announcements,
                "rejected": outcome.rejected,
                "deferred": outcome.deferred,
                "degraded": outcome.degraded,
                "cache_hit": outcome.cache_hit,
                "calls": outcome.calls,
                "issues": list(outcome.issues),
            },
        )


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #

class NightlyRunner:
    """Runs one logical nightly cycle over the configured adapters."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        adapters: Sequence[SourceAdapter],
        builder: ReleaseBuilder,
        publish_dir: str | Path | None = None,
        lease_path: str | Path | None = None,
        retries: int = 2,
        backoff_s: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
        verification: str = "operational",
        source_labels: Sequence[str] = (),
    ) -> None:
        self._conn = conn
        self._adapters = tuple(adapters)
        self._builder = builder
        self._publish_dir = Path(publish_dir) if publish_dir else None
        self._lease_path = Path(lease_path) if lease_path else Path(os.environ.get("XDG_CACHE_HOME", "/tmp")) / "randreport" / "nightly.lock"
        self._retries = retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._clock = clock
        self._verification = verification
        self._source_labels = tuple(source_labels)

    def run(self, *, now: datetime | None = None, publish: bool = True) -> RunReport:
        cutoff = now or self._clock()
        logical_date = logical_date_for(cutoff)
        outcomes: dict[str, SourceOutcome] = {}
        issues: list[str] = []

        with RunLease(self._lease_path):
            for adapter in self._adapters:
                outcome = self._run_source(adapter, cutoff=cutoff, logical_date=logical_date)
                outcomes[adapter.name] = outcome
                if outcome.status != "ok":
                    issues.append(f"source_{adapter.name}_{outcome.status}")

            statuses = {adapter.name: source_status(self._conn, adapter.name, now=cutoff) for adapter in self._adapters}
            freshness = overall_freshness(statuses.values())
            completeness = "complete" if all(status.coverage == "complete" for status in statuses.values()) else "partial"

            release: PublicRelease | None = None
            published = False
            try:
                release = self._builder.build(
                    self._conn,
                    cutoff=cutoff,
                    freshness=freshness,
                    verification=self._verification,
                    completeness=completeness,
                    source_labels=self._source_labels or tuple(adapter.name for adapter in self._adapters),
                    finance_data_present=self._finance_present(statuses),
                )
            except Exception as exc:  # noqa: BLE001 - never publish an unsafe revision
                repo.record_issue(
                    self._conn,
                    issue_id=f"release_build_failed:{logical_date}",
                    code="release_build_failed",
                    severity="error",
                    entity_type="release",
                    payload={"reason": str(exc)},
                    now=cutoff.isoformat(),
                )
                issues.append("release_build_failed")

            if release is not None and publish:
                repo.store_release(
                    self._conn,
                    release_id=release.release_id,
                    as_of=release.as_of.isoformat(),
                    schema_version=self._builder.schema_version,
                    sections=tuple(release.payload_json),
                    payload_text=release.combined_payload(),
                    built_at=cutoff.isoformat(),
                    make_current=True,
                )
                if self._publish_dir is not None:
                    publish_release(self._publish_dir, release.release_id, release.files())
                published = True

            repo.audit(
                self._conn,
                action="nightly_run",
                entity_type="release",
                entity_key=release.release_id if release else None,
                detail={"logical_date": logical_date, "freshness": freshness, "published": published},
                now=cutoff.isoformat(),
            )

        return RunReport(
            run_id=f"nightly_{logical_date}",
            logical_date=logical_date,
            cutoff=cutoff,
            sources=outcomes,
            statuses=statuses,
            release_id=release.release_id if release else None,
            published=published,
            freshness=freshness,
            completeness=completeness,
            issues=tuple(issues),
        )

    # --- helpers ----------------------------------------------------------- #

    def _run_source(self, adapter: SourceAdapter, *, cutoff: datetime, logical_date: str) -> SourceOutcome:
        run_id, _created = repo.start_source_run(
            self._conn, source=adapter.name, logical_date=logical_date, cutoff=cutoff.isoformat()
        )
        cursor = repo.get_cursor(self._conn, adapter.name)
        cursor_value = cursor[0] if cursor else None

        last_error: Exception | None = None
        outcome: SourceOutcome | None = None
        for attempt in range(self._retries + 1):
            try:
                outcome = adapter.sync(
                    conn=self._conn, cutoff=cutoff, logical_date=logical_date, cursor=cursor_value
                )
                break
            except Exception as exc:  # noqa: BLE001 - a source failure is contained
                last_error = exc
                if attempt < self._retries:
                    self._sleep(self._backoff_s * (attempt + 1))

        if outcome is None:
            repo.finish_source_run(
                self._conn,
                run_id=run_id,
                status="failed",
                error=str(last_error),
                now=cutoff.isoformat(),
            )
            repo.record_issue(
                self._conn,
                issue_id=f"source_failed:{adapter.name}:{logical_date}",
                code="source_failed",
                severity="error",
                entity_type="source",
                entity_key=adapter.name,
                payload={"reason": str(last_error)},
                now=cutoff.isoformat(),
            )
            return SourceOutcome(source=adapter.name, status="failed", error=str(last_error))

        repo.finish_source_run(
            self._conn,
            run_id=run_id,
            status=outcome.status,
            counts=outcome.counts,
            data_as_of=outcome.data_as_of,
            coverage=outcome.coverage,
            error=outcome.error,
            now=cutoff.isoformat(),
        )
        if outcome.status == "ok":
            repo.set_last_good(
                self._conn,
                source=adapter.name,
                run_id=run_id,
                payload=dict(outcome.payload),
                data_as_of=outcome.data_as_of or cutoff.isoformat(),
                coverage=outcome.coverage,
                now=cutoff.isoformat(),
            )
            if outcome.cursor is not None:
                current = repo.get_cursor(self._conn, adapter.name)
                revision = (current[1] + 1) if current else 1
                repo.set_cursor(self._conn, adapter.name, outcome.cursor, revision=revision, now=cutoff.isoformat())
        elif outcome.status == "partial":
            # keep last-good and the cursor: the next run resumes from the same point
            repo.record_issue(
                self._conn,
                issue_id=f"source_partial:{adapter.name}:{logical_date}",
                code="source_partial",
                severity="warning",
                entity_type="source",
                entity_key=adapter.name,
                payload={"reason": outcome.error},
                now=cutoff.isoformat(),
            )
        return outcome

    def _finance_present(self, statuses: Mapping[str, SourceStatus]) -> bool:
        zenmoney = statuses.get("zenmoney")
        if zenmoney is not None and zenmoney.last_success_at:
            return True
        return bool(repo.load_movements(self._conn))
