"""The nightly run: lease, per-source reliability, last-good and atomic publish.

Order of operations (docs/REQUIREMENTS.md section 9): fix the cutoff and run_id,
pull each source, validate and deduplicate, write facts and cursors in one
transaction, compute the aggregates, build a safe revision, publish atomically.
A failure of one source never zeroes another, and a new payload that fails a
safety check is not published — the previous safe release keeps serving.
"""

from __future__ import annotations

import fcntl
import dataclasses
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..connectors.teamup import CHANGELOG_MAX_AGE_S, TeamupClient
from ..connectors.telegram import ChannelSubscriberPoint, HistoryUnavailable, RawPost, TelegramClient
from ..connectors.zenmoney import ZenmoneyClient, account_balances
from ..domain.finance import AccountBalance, ScopePolicy
from ..domain.timeframes import MSK, msk_day
from ..inbox.store import INBOX_SOURCE, MAX_BATCH, commit_posts, inbox_counts, pending_posts
from ..news.extract import BatchOutcome, NewsExtractor
from ..store import repository as repo
from .pipeline import PublicRelease, ReleaseBuilder
from .publish import publish_release
from .schedule import logical_date_for, utc_now
from .status import SourceStatus, overall_freshness, source_status


class SourceError(RuntimeError):
    """A source that could not be read within the retry budget."""


def _iso_epoch(value: str | None) -> int | None:
    """The unix timestamp of a stored ISO cursor, or ``None`` if it is not one."""

    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return None


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
        balance_reader: Callable[[Any, datetime], Sequence[AccountBalance]] | None = None,
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
            balances = tuple(self._balance_reader(diff, cutoff))
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


class PolicyBalanceReader:
    """Snapshot the accounts of a diff with an explicit, policy-scoped liquidity.

    An account confirmed to belong to the public contour is *not* automatically
    liquid: the account's own type decides, and the scope only says whether the
    money is public at all (docs/DOMAIN.md section 6).
    """

    def __init__(self, policy: ScopePolicy) -> None:
        self._policy = policy

    def __call__(self, diff: Any, cutoff: datetime) -> Sequence[AccountBalance]:
        return account_balances(
            tuple(getattr(diff, "account_rows", ()) or ()),
            policy=self._policy,
            as_of=msk_day(cutoff),
        )


class TeamupAdapter:
    """Read-only Teamup sync: a wide date window plus the changelog for deletions."""

    name = "teamup"

    def __init__(self, client: TeamupClient, *, window_days: int = 400) -> None:
        self._client = client
        self._window_days = window_days

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        start, end = self._client.default_window(cutoff, depth_days=self._window_days)
        # The window is always read in full: it is what returns the *instances* of a
        # repeating series, and the documented change filter cannot be combined with
        # a date range (the API answers 400), nor does it return instances.
        window = self._client.fetch_window(start_date=start, end_date=end)
        stamp = cutoff.isoformat()
        counting = repo.upsert_occurrences(conn, window.occurrences, now=stamp)

        # An explicit deletion is only visible in the changelog: a record deleted on
        # the source simply disappears from the window, where it looks exactly like
        # source retention. The changelog covers at most 30 days, so it is only
        # asked for a cursor that is still inside it; anything older is served by the
        # window plus the local archive.
        deleted_ids = list(window.deleted_ids)
        changelog = "not_queried"
        cursor_epoch = _iso_epoch(cursor)
        run_epoch = int(cutoff.timestamp())
        if cursor_epoch is not None:
            age = self._client.changelog_age_seconds(cursor_epoch, now=run_epoch)
            if 0 <= age <= CHANGELOG_MAX_AGE_S:
                changelog = "queried"
                deleted_ids.extend(
                    self._client.fetch_deleted_since(modified_since=cursor_epoch, now=run_epoch)
                )
            else:
                changelog = "cursor_older_than_the_changelog"

        deleted = repo.mark_occurrences_deleted(
            conn, calendar_id=self._client.calendar_id, ids=tuple(dict.fromkeys(deleted_ids)), now=stamp
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
                "deleted_from_source": len(deleted_ids),
                "marked_deleted": deleted,
                "archived_by_retention": archived,
                "pages": window.counts.pages,
                "changelog": changelog,
            },
        )


class TelegramAdapter:
    """The daily subscriber snapshot, and only that (docs/REQUIREMENTS.md section 8).

    The snapshot calls ``getChatMemberCount``; the post history and the news
    extraction are separate concerns with their own status.  Reading them apart is
    the point: a history source that is unavailable (no approved MTProto session,
    or an empty webhook inbox) must never make the subscriber count look broken,
    and a snapshot error must never be reported as a news failure.

    When a read-only history source *is* configured, its posts are archived here
    as well; a failure of that pull is recorded as its own issue
    (``telegram_history_unavailable``) and reported in the payload, but it does
    not degrade this source — the snapshot already succeeded on its own. With no
    history source configured at all (``HistoryUnavailable``) the pull is simply
    not attempted: that is the documented normal gap of this section, so it is
    reported as ``not_configured`` in the payload and in the coverage — never as
    a nightly issue.
    """

    name = "telegram"

    def __init__(self, client: TelegramClient, *, chat_id: str, channel_id: str, batch: int = 100) -> None:
        self._client = client
        self._chat_id = chat_id
        self._channel_id = channel_id
        self._batch = batch

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        stamp = cutoff.isoformat()
        snapshot_error: str | None = None
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
            snapshot_error = str(exc)
            repo.record_issue(
                conn,
                issue_id=f"telegram_snapshot_missing:{logical_date}",
                code="telegram_snapshot_missing",
                severity="warning",
                entity_type="channel",
                entity_key=self._chat_id,
                payload={"reason": snapshot_error},
                now=stamp,
            )

        posts: tuple[RawPost, ...] = ()
        history_error: str | None = None
        history_status = "ok"
        try:
            posts = self._client.pull_posts(channel_id=self._channel_id, since_id=cursor, limit=self._batch)
        except HistoryUnavailable:
            # no read-only history source is configured: that is the documented
            # normal state of this section (a webhook inbox cannot prove
            # completeness), so the coverage is partial and the payload says
            # ``not_configured`` — no error and no nightly issue, since an
            # always-on issue every night would be pure noise
            history_status = "not_configured"
        except Exception as exc:  # noqa: BLE001 - a history gap is not a snapshot error
            history_error = str(exc)
            history_status = "unavailable"
            repo.record_issue(
                conn,
                issue_id=f"telegram_history_unavailable:{logical_date}",
                code="telegram_history_unavailable",
                severity="warning",
                entity_type="channel",
                entity_key=self._channel_id,
                payload={"reason": history_error},
                now=stamp,
            )

        counts = repo.upsert_posts(conn, posts, now=stamp) if posts else repo.UpsertCounts()
        next_cursor = posts[-1].message_id if posts else cursor
        # only the snapshot decides this source's status: it is the source's one job
        status = "ok" if snapshot_error is None else "partial"
        coverage = "complete" if snapshot_error is None else "partial"
        return SourceOutcome(
            source=self.name,
            status=status,
            counts=counts,
            # the runner advances the cursor only for an "ok" source; a partial
            # one resumes from the same point (see NightlyRunner._run_source)
            cursor=next_cursor,
            data_as_of=None if snapshot_error is not None and subscribers is None else stamp,
            coverage=coverage,
            payload={
                "snapshot": snapshot_result,
                "subscribers": subscribers,
                "snapshot_error": snapshot_error,
                "history": history_status,
                "history_error": history_error,
                "posts_read": len(posts),
            },
            error=None if snapshot_error is None else f"snapshot: {snapshot_error}",
        )


class InboxAdapter:
    """Commit the durable webhook inbox into the post archive (section 8).

    The webhook only *receives*; this adapter is the only thing that turns what
    was received into archived posts, and it runs inside the nightly cycle.  At
    most :data:`randreport.inbox.store.MAX_BATCH` messages are committed per pass;
    the rest stay pending and are visible in the payload.

    Coverage is reported honestly: an inbox can never prove that nothing was
    missed, because a delivery may simply not have happened.  It is ``partial``
    unless a read-only history pull (an approved MTProto session) covers the same
    window, and the release then shows a news gap instead of a complete section.
    """

    name = INBOX_SOURCE

    def __init__(self, *, batch: int = MAX_BATCH, history_verified: bool = False) -> None:
        self._batch = batch
        self._history_verified = history_verified

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        stamp = cutoff.isoformat()
        pending = pending_posts(conn, limit=self._batch)
        archived = tuple(post.to_raw_post() for post in pending)
        counts = repo.upsert_posts(conn, archived, now=stamp) if archived else repo.UpsertCounts()
        committed = commit_posts(conn, pending, now=cutoff)
        state = inbox_counts(conn)
        left = state["pending_messages"]
        coverage = "complete" if self._history_verified else "partial"
        reasons = () if self._history_verified else ("inbox_cannot_prove_completeness",)
        if left:
            reasons = (*reasons, "batch_truncated")
            coverage = "partial"
            repo.record_issue(
                conn,
                issue_id=f"news_inbox_backlog:{logical_date}",
                code="news_inbox_backlog",
                severity="warning",
                entity_type="news",
                payload={"pending_messages": left, "batch": self._batch},
                now=stamp,
            )
        return SourceOutcome(
            source=self.name,
            status="ok",
            counts=counts,
            cursor=None,  # the inbox marker is the cursor; there is nothing to advance
            data_as_of=stamp,
            coverage=coverage,
            payload={
                "committed_messages": len(pending),
                "rows_marked": committed,
                "pending_messages": left,
                "archive_new": counts.new,
                "archive_changed": counts.changed,
                "history_verified": self._history_verified,
                "coverage_reasons": list(reasons),
                "received_total": state["messages"],
            },
        )


class NewsAdapter:
    """One daily LLM pass over new/changed public posts.

    Coverage is what makes this section honest.  It is ``complete`` only when
    every one of these holds: a read-only history pull covers the window (an
    inbox alone cannot), a provider is configured, and the pass was not degraded,
    deferred or truncated by the batch limit.  Otherwise the section is still
    served with ``status=ok`` (a missing LLM never breaks the site) but it is
    marked ``partial`` with an explicit reason, so an empty inbox is never
    published as "the channel had nothing to say".
    """

    name = "news"

    def __init__(
        self,
        extractor: NewsExtractor,
        *,
        batch_size: int = 200,
        history_verified: bool = False,
    ) -> None:
        self._extractor = extractor
        self._batch_size = batch_size
        self._history_verified = history_verified

    def _pending(self, conn: sqlite3.Connection, cursor: str | None) -> tuple[tuple[RawPost, ...], bool]:
        """New/changed posts, and whether the batch limit hid some of them."""

        posts = repo.archived_posts(conn)
        if cursor is None:
            wanted = list(posts)
        else:
            wanted = []
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
        truncated = len(wanted) > self._batch_size
        return tuple(wanted[: self._batch_size]), truncated

    def coverage_reasons(self, outcome: BatchOutcome, *, truncated: bool) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self._history_verified:
            # a webhook inbox records what arrived, never what was missed
            reasons.append("history_gap_possible")
        if self._extractor.provider is None:
            reasons.append("llm_unavailable")
        if outcome.deferred:
            reasons.append("deferred")
        if outcome.degraded:
            reasons.append("degraded")
        if truncated:
            reasons.append("batch_truncated")
        if outcome.posts == 0:
            reasons.append("no_posts_received")
        return tuple(reasons)

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        pending, truncated = self._pending(conn, cursor)
        if truncated:
            repo.record_issue(
                conn,
                issue_id=f"news_batch_truncated:{logical_date}",
                code="news_batch_truncated",
                severity="warning",
                entity_type="news",
                payload={"batch_size": self._batch_size},
                now=cutoff.isoformat(),
            )
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
        reasons = self.coverage_reasons(outcome, truncated=truncated)
        coverage = "complete" if not reasons else "partial"
        # The reasons are reported in the source payload and in the release's
        # completeness, *not* as an open issue: a missing LLM or an inbox that
        # cannot prove completeness is the normal state of this section, and an
        # open issue would permanently block an unrelated monthly report draft.
        # Only real interruptions (a deferred batch, a rate limit, a truncated
        # batch, a growing backlog) raise an issue, and those already do.
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
                "rate_limited": outcome.rate_limited,
                "cache_hit": outcome.cache_hit,
                "calls": outcome.calls,
                "posts": outcome.posts,
                "batch_truncated": truncated,
                "coverage": coverage,
                "coverage_reasons": list(reasons),
                "issues": list(outcome.issues),
            },
        )


# --------------------------------------------------------------------------- #
# Forwarding counters over MTProto (Telethon, bot login)                       #
# --------------------------------------------------------------------------- #

class TelegramStatsAdapter(SourceAdapter):
    """Re-measure forwards/views of the rolling 30-day window posts.

    The window is 30 calendar days by Moscow time, including the current day,
    and the message ids come from the archive the other adapters maintain — the
    reader never scans channel history.  Every sync re-measures *all* window
    posts, because forward counts of old announcements keep changing.  A Telegram
    failure saves nothing for the unanswered posts and never wipes the values a
    previous pass stored; the site keeps rendering the last measured numbers.
    """

    name = "telegram_stats"

    def __init__(self, reader: Any, *, window_days: int = 30) -> None:
        if window_days < 1:
            raise ValueError("window_days must be positive")
        self._reader = reader
        self._window_days = window_days

    def window(self, cutoff: datetime) -> tuple[date, date]:
        """The inclusive Moscow-day window this pass measures, ``(from, to)``."""

        end = msk_day(cutoff)
        return end - timedelta(days=self._window_days - 1), end

    def sync(self, *, conn: sqlite3.Connection, cutoff: datetime, logical_date: str, cursor: str | None) -> SourceOutcome:
        window_start, window_end = self.window(cutoff)
        posts = [
            post
            for post in repo.archived_posts(conn)
            if not post.deleted and window_start <= msk_day(post.posted_at) <= window_end
        ]
        ids = sorted({post.message_id for post in posts}, key=int)
        stamp = cutoff.isoformat()
        base_payload: dict[str, Any] = {
            "window_from": window_start.isoformat(),
            "window_to": window_end.isoformat(),
        }
        if not ids:
            # an empty window is a successful measurement of nothing: the last
            # stored counters stay, the source reports a clean pass
            return SourceOutcome(
                source=self.name,
                status="ok",
                cursor=None,
                data_as_of=stamp,
                coverage="complete",
                payload={**base_payload, "posts": 0, "updated": 0, "latest_fetch": repo.latest_counter_fetch(conn)},
            )

        report = self._reader.fetch(ids)
        counts = repo.upsert_post_counters(conn, report.counters, now=stamp)
        reasons: list[str] = []
        if report.failed_batches:
            reasons.append("batch_failures")
        if report.flood_wait_s is not None:
            reasons.append(f"flood_wait_{int(report.flood_wait_s)}s")
        coverage = "complete" if not reasons else "partial"
        payload: dict[str, Any] = {
            **base_payload,
            "posts": len(ids),
            "measured": len(report.fetched_ids),
            "deleted_or_missing": len(report.skipped_ids),
            "failed": len(report.failed_batches),
            "updated": counts.changed + counts.new,
            "flood_wait_s": report.flood_wait_s,
            "latest_fetch": repo.latest_counter_fetch(conn),
            "coverage": coverage,
            "coverage_reasons": reasons,
        }
        return SourceOutcome(
            source=self.name,
            status="ok" if not reasons else "partial",
            counts=counts,
            cursor=None,  # the window is re-derived every pass; there is no cursor
            data_as_of=stamp,
            coverage=coverage,
            payload=payload,
            error="; ".join(reasons) or None,
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
                    publish_release(self._publish_dir, release.release_id, self._release_files(release))
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
            # a source that reads successfully is no longer failing: close its
            # earlier per-date lifecycle issues, which nothing else can close
            resolved = repo.resolve_source_issues(self._conn, source=adapter.name, now=cutoff.isoformat())
            if resolved:
                outcome = dataclasses.replace(outcome, payload={**outcome.payload, "issues_resolved": len(resolved)})
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

    def _release_files(self, release: PublicRelease) -> dict[str, str]:
        """The release files plus the allowlisted report archive (no DB at read time).

        Publishing the archive with the release is what keeps the public process
        free of the private database: ``/reports/*`` is served from these files.
        """

        from ..web import reports as reports_module

        files = release.files()
        archive = reports_module.public_archive(self._conn)
        files["reports.json"] = json.dumps(archive, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return files
