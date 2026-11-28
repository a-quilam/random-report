"""E02 — an opaque source revision must never stop the import.

docs/REQUIREMENTS.md sections 7, 9 and 10 with the E02 row of the acceptance
matrix. Teamup reports ``version`` as an opaque token (observed in real staging
as a hex string), so it is stored as an opaque token:

* never parsed into a number (no ``int(raw["version"])``, no collapse to 1),
* never read as a timestamp,
* never used to order two revisions of the same occurrence — an opaque token
  orders nothing, so the last delivery of a key wins.

The clauses of the card are covered one by one: edits, moves, tombstone,
retention, repeated deliveries, invalidation of checks and the separation of the
secret calendar link from the stable internal calendar identity under key
rotation. Everything is synthetic: invented ids, invented calendar keys, no
credential, no real payload.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.admin import checks, desk, diagnostics  # noqa: E402
from randreport.connectors.teamup import DEFAULT_CALENDAR_ID, TeamupClient, TeamupProtocolError  # noqa: E402
from randreport.connectors.transport import json_response  # noqa: E402
from randreport.domain.events import UNVERSIONED_REVISION, event_stats, select_completed  # noqa: E402
from randreport.store import repository as repo  # noqa: E402
from randreport.store import missing_tables  # noqa: E402
from randreport.store.db import transaction  # noqa: E402
from randreport.store.migrations import MIGRATIONS  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests import fixtures_ingest as ig  # noqa: E402

SECRET_ONE = "synthetic-secret-calendar-key-aaaa"
SECRET_TWO = "synthetic-secret-calendar-key-bbbb"
INTERNAL_ID = "cal_community_synthetic"
WINDOW = (date(2025, 1, 1), date(2025, 12, 31))


def page(events):
    return {"events": list(events), "pages": {"next": None}}


def db_dump(conn) -> str:
    """Every schema text and every row value of the private store, as one string."""

    parts: list[str] = []
    for row in conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall():
        parts.append(str(row["name"]))
        parts.append(str(row["sql"]))
    for table in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for record in conn.execute(f"SELECT * FROM {table['name']}").fetchall():
            parts.extend("" if value is None else str(value) for value in tuple(record))
    return "\n".join(parts)


class OpaqueRevisionTokenTests(unittest.TestCase):
    """The connector accepts whatever token the API sends instead of a number."""

    def fetch(self, events, **kwargs):
        client, _transport = ig.teamup(pages=[page(events)], **kwargs)
        return client.fetch_window(start_date=WINDOW[0], end_date=WINDOW[1])

    def test_a_hex_revision_is_accepted_and_kept_verbatim(self):
        window = self.fetch([ig.teamup_event("occ_hex", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", version="6a1f2c9d4b")])
        self.assertEqual(1, len(window.occurrences))
        self.assertEqual("6a1f2c9d4b", window.occurrences[0].revision)

    def test_a_token_with_leading_zeros_is_not_coerced_into_a_number(self):
        window = self.fetch([ig.teamup_event("occ_pad", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", version="0007")])
        self.assertEqual("0007", window.occurrences[0].revision)

    def test_a_numeric_json_revision_becomes_the_same_text_token(self):
        window = self.fetch([ig.teamup_event("occ_num", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", version=7)])
        self.assertEqual("7", window.occurrences[0].revision)

    def test_a_missing_revision_gets_the_documented_token_and_not_one(self):
        window = self.fetch([ig.teamup_event("occ_absent", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", version=None)])
        self.assertEqual(UNVERSIONED_REVISION, window.occurrences[0].revision)
        self.assertNotEqual("1", window.occurrences[0].revision)

    def test_a_token_that_looks_like_an_epoch_is_not_read_as_time(self):
        window = self.fetch([ig.teamup_event("occ_epoch", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", version="1754000000")])
        occurrence = window.occurrences[0]
        self.assertEqual("1754000000", occurrence.revision)
        self.assertEqual("2025-07-03", occurrence.day().isoformat())
        self.assertEqual("2025-07-03T18:00:00+03:00", occurrence.start.isoformat())

    def test_a_non_scalar_revision_is_a_protocol_error(self):
        with self.assertRaises(TeamupProtocolError):
            self.fetch([ig.teamup_event("occ_bad", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", version={"hex": "6a1f"})])

    def test_a_tombstone_needs_no_revision_token(self):
        window = self.fetch([{"id": "occ_tomb", "subcalendar_ids": ["sub_open"], "delete_dt": "2025-07-03T18:00:00+03:00"}])
        self.assertEqual(("occ_tomb",), window.deleted_ids)
        self.assertEqual((), window.occurrences)

    def test_a_tombstone_without_an_id_is_a_protocol_error(self):
        with self.assertRaises(TeamupProtocolError):
            self.fetch([{"subcalendar_ids": ["sub_open"], "delete_dt": "2025-07-03T18:00:00+03:00"}])


class UnorderedRevisionTests(unittest.TestCase):
    """A revision token orders nothing: the last delivery of a key wins."""

    MAPPING = fx.event_mapping()
    CUTOFF = fx.msk(2025, 7, 31, 3, 0)

    def select(self, occurrences):
        return select_completed(occurrences, mapping=self.MAPPING, cutoff=self.CUTOFF)

    def test_the_last_delivery_wins_even_when_its_token_sorts_lower(self):
        occurrences = [
            fx.occurrence("occ_move", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="ffffffff"),
            fx.occurrence("occ_move", fx.msk(2025, 7, 4, 19, 0), fx.msk(2025, 7, 4, 21, 0), revision="00000001"),
        ]
        selection = self.select(occurrences)
        self.assertEqual(1, event_stats(selection, cutoff=self.CUTOFF).total)
        self.assertEqual("2025-07-04", selection.completed[0].occurrence.start.astimezone(fx.MSK).date().isoformat())
        self.assertEqual(["00000001"], [item.occurrence.revision for item in selection.completed])
        self.assertEqual(["ffffffff"], [o.revision for o in selection.superseded])
        self.assertEqual(1, selection.duplicates_collapsed)

    def test_revisions_are_logged_in_delivery_order_not_sorted_by_token(self):
        occurrences = [
            fx.occurrence("occ_log", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="ffffffff"),
            fx.occurrence("occ_log", fx.msk(2025, 7, 4, 19, 0), fx.msk(2025, 7, 4, 21, 0), revision="00000001"),
        ]
        self.assertEqual(["ffffffff", "00000001"], [o.revision for o in self.select(occurrences).revision_log])

    def test_a_repeated_delivery_of_the_same_token_is_collapsed(self):
        occurrences = [
            fx.occurrence("occ_repeat", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="6a1f"),
            fx.occurrence("occ_repeat", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="6a1f"),
        ]
        selection = self.select(occurrences)
        self.assertEqual(1, event_stats(selection, cutoff=self.CUTOFF).total)
        self.assertEqual(1, selection.duplicates_collapsed)
        self.assertEqual(2, len(selection.revision_log))

    def test_a_tombstone_delivered_last_wins_over_an_unordered_live_token(self):
        occurrences = [
            fx.occurrence("occ_gone", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="ffffffff"),
            fx.occurrence("occ_gone", fx.msk(2025, 7, 3, 18, 0), fx.msk(2025, 7, 3, 20, 0), revision="00000001", deleted=True),
        ]
        selection = self.select(occurrences)
        self.assertEqual(0, event_stats(selection, cutoff=self.CUTOFF).total)
        self.assertEqual(["occ_gone"], [o.occurrence_id for o in selection.deleted])
        self.assertEqual("00000001", selection.deleted[0].revision)


class OpaqueRevisionEndToEndTests(unittest.TestCase):
    """raw -> SQLite -> sync -> public with two different opaque revisions."""

    FIRST = "6a1f2c9d4b"
    SECOND = "e07d5b31aa"
    MOVED = "b90c47"

    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def sync(self, events, *, client=None, transport=None, now=ig.NOW):
        if client is None:
            client, transport = ig.teamup(pages=[page(events)], calendar_id=INTERNAL_ID)
        else:
            transport.handler = ig.teamup_pages([page(events)])
        return ig.run_once(self.conn, [ig.teamup_adapter(client)], now=now, lease=self.lease)

    def stored(self):
        rows = self.conn.execute("SELECT calendar_id, occurrence_id, start_utc, revision FROM calendar_occurrences").fetchall()
        return [(str(row["calendar_id"]), str(row["occurrence_id"]), str(row["start_utc"]), str(row["revision"])) for row in rows]

    def events_payload(self):
        return json.loads(repo.current_release(self.conn)["payload"])["events"]

    def check_key(self):
        row = self.conn.execute("SELECT calendar_id || '|' || occurrence_id AS key FROM calendar_occurrences").fetchone()
        return str(row["key"])

    def event(self, start, end, *, revision):
        return ig.teamup_event("occ_opaque", start, end, title="Открытая лекция", version=revision)

    def test_two_opaque_revisions_and_a_repeat_travel_the_whole_path(self):
        first = self.sync([self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.FIRST)])
        self.assertEqual("ok", first.sources["teamup"].status)
        self.assertEqual(1, first.sources["teamup"].counts.new)
        self.assertEqual([(INTERNAL_ID, "occ_opaque", "2025-07-03T15:00:00+00:00", self.FIRST)], self.stored())
        self.assertEqual(["2025-07-03"], [item["on"] for item in self.events_payload()["public_events"]])
        self.assertEqual("text", self.conn.execute("SELECT typeof(revision) AS kind FROM calendar_occurrences").fetchone()["kind"])

        checks.verify(self.conn, entity_type="calendar_occurrence", entity_key=self.check_key(), actor="synthetic_admin")
        self.assertEqual(checks.VERIFIED, checks.effective_state(self.conn, "calendar_occurrence", self.check_key()))

        # the source edited the record: a new opaque revision, same visible fields
        edited = self.sync([self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.SECOND)])
        self.assertEqual(1, edited.sources["teamup"].counts.changed)
        self.assertEqual([(INTERNAL_ID, "occ_opaque", "2025-07-03T15:00:00+00:00", self.SECOND)], self.stored())
        self.assertEqual(1, self.events_payload()["metrics"]["total_events"])
        self.assertEqual(checks.STALE, checks.effective_state(self.conn, "calendar_occurrence", self.check_key()))

        # a repeated delivery of the same revision changes nothing
        repeated = self.sync([self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.SECOND)])
        self.assertEqual(1, repeated.sources["teamup"].counts.unchanged)
        self.assertEqual(1, len(self.stored()))
        self.assertEqual(1, self.events_payload()["metrics"]["total_events"])
        self.assertEqual(checks.STALE, checks.effective_state(self.conn, "calendar_occurrence", self.check_key()))

        # a move under a third opaque revision, then a repeat of that very payload
        moved = self.sync([self.event("2025-07-04T19:00:00+03:00", "2025-07-04T21:00:00+03:00", revision=self.MOVED)])
        self.assertEqual(1, moved.sources["teamup"].counts.changed)
        self.assertEqual([(INTERNAL_ID, "occ_opaque", "2025-07-04T16:00:00+00:00", self.MOVED)], self.stored())
        self.assertEqual(["2025-07-04"], [item["on"] for item in self.events_payload()["public_events"]])
        self.assertEqual(1, self.events_payload()["metrics"]["total_events"])

        checks.verify(self.conn, entity_type="calendar_occurrence", entity_key=self.check_key(), actor="synthetic_admin")
        self.assertEqual(checks.VERIFIED, checks.effective_state(self.conn, "calendar_occurrence", self.check_key()))
        again = self.sync([self.event("2025-07-04T19:00:00+03:00", "2025-07-04T21:00:00+03:00", revision=self.MOVED)])
        self.assertEqual(1, again.sources["teamup"].counts.unchanged)
        self.assertEqual(checks.VERIFIED, checks.effective_state(self.conn, "calendar_occurrence", self.check_key()))
        self.assertEqual(1, self.events_payload()["metrics"]["total_events"])

    def test_a_single_window_with_two_opaque_revisions_of_one_key_keeps_one_row(self):
        report = self.sync(
            [
                self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.FIRST),
                self.event("2025-07-04T19:00:00+03:00", "2025-07-04T21:00:00+03:00", revision=self.SECOND),
            ]
        )
        self.assertEqual("ok", report.sources["teamup"].status)
        self.assertEqual([(INTERNAL_ID, "occ_opaque", "2025-07-04T16:00:00+00:00", self.SECOND)], self.stored())
        self.assertEqual(1, self.events_payload()["metrics"]["total_events"])

    def test_a_tombstone_with_an_opaque_revision_keeps_the_row_and_is_idempotent(self):
        self.sync([self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.FIRST)])
        tombstone = ig.teamup_event("occ_opaque", "2025-07-03T18:00:00+03:00", None, delete=True, version=self.SECOND)
        first = self.sync([tombstone])
        self.assertEqual(1, first.sources["teamup"].payload["marked_deleted"])
        row = self.conn.execute("SELECT deleted, revision FROM calendar_occurrences").fetchone()
        self.assertEqual(1, row["deleted"])
        self.assertEqual(self.FIRST, str(row["revision"]))
        self.assertEqual(0, self.events_payload()["metrics"]["total_events"])
        self.assertEqual([], self.events_payload()["public_events"])

        second = self.sync([tombstone])
        self.assertEqual(0, second.sources["teamup"].payload["marked_deleted"])
        self.assertEqual(1, len(self.stored()))

    def test_retention_keeps_the_opaque_token_and_does_not_reset_it(self):
        self.sync([self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.FIRST)])
        report = self.sync([])
        self.assertEqual(1, report.sources["teamup"].payload["archived_by_retention"])
        row = self.conn.execute("SELECT local_archive, source_present, revision FROM calendar_occurrences").fetchone()
        self.assertEqual(1, row["local_archive"])
        self.assertEqual(0, row["source_present"])
        self.assertEqual(self.FIRST, str(row["revision"]))
        self.assertEqual(1, self.events_payload()["metrics"]["local_archive_events"])

    def test_a_record_the_source_returns_again_leaves_the_archive(self):
        # a later nightly cycle: the release id is content-addressed including
        # as_of, so a same-day re-derivation would collide with the release that
        # was already published (and is never rewritten)
        day = ig.NOW + timedelta(days=1)
        self.sync([self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.FIRST)])
        self.sync([], now=day)
        self.sync([self.event("2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", revision=self.FIRST)], now=day + timedelta(days=1))
        row = self.conn.execute("SELECT local_archive, source_present, revision FROM calendar_occurrences").fetchone()
        self.assertEqual(0, row["local_archive"])
        self.assertEqual(1, row["source_present"])
        self.assertEqual(self.FIRST, str(row["revision"]))
        self.assertEqual(0, self.events_payload()["metrics"]["local_archive_events"])
        self.assertEqual(1, self.events_payload()["metrics"]["total_events"])


class CalendarIdentityTests(unittest.TestCase):
    """The secret calendar link and the stable internal identity are separate."""

    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def event(self, *, revision="9c1a04"):
        return ig.teamup_event("occ_identity", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00", title="Открытая лекция", version=revision)

    def test_the_secret_key_is_not_the_internal_identity(self):
        client, _transport = ig.teamup(pages=[page([self.event()])], calendar_key=SECRET_ONE, calendar_id=INTERNAL_ID)
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        self.assertNotEqual(SECRET_ONE, client.calendar_id)
        self.assertEqual(INTERNAL_ID, client.calendar_id)
        row = self.conn.execute("SELECT calendar_id FROM calendar_occurrences").fetchone()
        self.assertEqual(INTERNAL_ID, str(row["calendar_id"]))
        self.assertNotIn(SECRET_ONE, db_dump(self.conn))
        self.assertNotIn(SECRET_ONE, json.dumps(desk.events_rows(self.conn), ensure_ascii=False, default=str))
        self.assertNotIn(SECRET_ONE, json.dumps(diagnostics.collect(self.conn), ensure_ascii=False, default=str))
        self.assertNotIn(SECRET_ONE, repo.current_release(self.conn)["payload"])

    def test_the_secret_key_is_never_accepted_as_the_internal_identity(self):
        with self.assertRaises(ValueError):
            TeamupClient(ig.RecordingTransport(), calendar_key=SECRET_ONE, calendar_id=SECRET_ONE)

    def test_the_default_internal_identity_is_stable_and_not_the_key(self):
        client, _transport = ig.teamup(pages=[page([])], calendar_key=SECRET_ONE)
        self.assertEqual(DEFAULT_CALENDAR_ID, client.calendar_id)
        self.assertNotIn(SECRET_ONE, client.calendar_id)

    def test_rotating_the_secret_key_keeps_one_calendar_and_its_history(self):
        first, transport = ig.teamup(pages=[page([self.event()])], calendar_key=SECRET_ONE, calendar_id=INTERNAL_ID)
        ig.run_once(self.conn, [ig.teamup_adapter(first)], lease=self.lease)
        checks.verify(self.conn, entity_type="calendar_occurrence", entity_key=f"{INTERNAL_ID}|occ_identity", actor="synthetic_admin")

        rotated = TeamupClient(transport, calendar_key=SECRET_TWO, calendar_id=INTERNAL_ID)
        transport.handler = ig.teamup_pages([page([self.event()])])
        report = ig.run_once(self.conn, [ig.teamup_adapter(rotated)], lease=self.lease)

        self.assertEqual("ok", report.sources["teamup"].status)
        rows = self.conn.execute("SELECT calendar_id, revision FROM calendar_occurrences").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(INTERNAL_ID, str(rows[0]["calendar_id"]))
        self.assertEqual("9c1a04", str(rows[0]["revision"]))
        self.assertEqual(checks.VERIFIED, checks.effective_state(self.conn, "calendar_occurrence", f"{INTERNAL_ID}|occ_identity"))
        payload = json.loads(repo.current_release(self.conn)["payload"])["events"]
        self.assertEqual(1, payload["metrics"]["total_events"])
        self.assertNotIn(SECRET_ONE, db_dump(self.conn))
        self.assertNotIn(SECRET_TWO, db_dump(self.conn))

    def test_two_internal_identities_are_two_calendars(self):
        one, _t1 = ig.teamup(pages=[page([self.event()])], calendar_key=SECRET_ONE, calendar_id="cal_first")
        ig.run_once(self.conn, [ig.teamup_adapter(one)], lease=self.lease)
        two, _t2 = ig.teamup(pages=[page([self.event(revision="1122ff")])], calendar_key=SECRET_TWO, calendar_id="cal_second")
        ig.run_once(self.conn, [ig.teamup_adapter(two)], lease=self.lease)
        rows = self.conn.execute("SELECT calendar_id, revision FROM calendar_occurrences ORDER BY calendar_id").fetchall()
        self.assertEqual([("cal_first", "9c1a04"), ("cal_second", "1122ff")], [(str(r["calendar_id"]), str(r["revision"])) for r in rows])


class RevisionColumnMigrationTests(unittest.TestCase):
    """An existing store keeps its rows and stores an opaque token verbatim."""

    def _upgraded_store(self):
        """A store at schema 3 with one occurrence whose revision is an integer."""

        conn = ig.connect()
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for migration in MIGRATIONS:
            if migration.version >= 4:
                break
            with transaction(conn):
                for statement in migration.statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, "2025-07-31T00:00:00+00:00"),
                )
        conn.execute(
            """
            INSERT INTO calendar_occurrences (
                calendar_id, occurrence_id, series_id, start_utc, end_utc, all_day,
                subcalendar_ids, title, raw_type, revision, deleted, source_present,
                local_archive, content_hash, ingested_at
            ) VALUES ('cal_community_synthetic', 'occ_old', NULL, '2025-07-03T15:00:00+00:00',
                      '2025-07-03T17:00:00+00:00', 0, '["sub_open"]', 'событие', NULL, 1, 0, 1, 0,
                      'hash_of_occ_old', '2025-07-31T00:00:00+00:00')
            """
        )
        # the merged release applies the later migrations to a schema-3 store:
        # 4 makes the revision TEXT, 5 adds the passive Telegram inbox, 6 the
        # durable getUpdates offset, 7 the reversible report exclusions, 8 the
        # forwarding counters of the post archive
        self.assertEqual((4, 5, 6, 7, 8), ig.apply_migrations(conn))
        return conn

    def test_an_existing_row_keeps_its_revision_as_text(self):
        conn = self._upgraded_store()
        rows = repo.load_occurrences(conn)
        self.assertEqual(["1"], [o.revision for o in rows])
        self.assertEqual("text", conn.execute("SELECT typeof(revision) AS kind FROM calendar_occurrences").fetchone()["kind"])
        indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("idx_calendar_occurrences_start", indexes)
        self.assertEqual((), missing_tables(conn))

    def test_a_token_the_integer_column_would_rewrite_round_trips_verbatim(self):
        conn = self._upgraded_store()
        tokens = ("0007", "12345678901234567890")
        occurrences = [
            fx.occurrence(
                f"occ_{index}",
                fx.msk(2025, 7, 3, 18, 0),
                fx.msk(2025, 7, 3, 20, 0),
                calendar="cal_community_synthetic",
                revision=token,
            )
            for index, token in enumerate(tokens)
        ]
        repo.upsert_occurrences(conn, occurrences)
        loaded = {occurrence.occurrence_id: occurrence.revision for occurrence in repo.load_occurrences(conn)}
        self.assertEqual({"occ_0": "0007", "occ_1": "12345678901234567890", "occ_old": "1"}, loaded)


class TeamupChangelogTests(unittest.TestCase):
    """The change filter is a separate call, because it cannot be combined.

    The real API answers 400 "Invalid request" to a request that carries both a
    date range and ``modifiedSince`` (documented: the changed-events call takes no
    date range), so the nightly cycle used to succeed only on its *first* run and
    fail on every run after it, once a cursor existed. These tests pin the fixed
    behaviour against a fixture server that reproduces the real refusal.
    """

    def setUp(self):
        self.conn = ig.fresh_conn()
        self.tmp = tempfile.TemporaryDirectory()
        self.lease = str(Path(self.tmp.name) / "nightly.lock")
        self.requests: list[Any] = []

    def tearDown(self):
        self.tmp.cleanup()
        self.conn.close()

    def _handler(self, events, *, deleted=(), timestamp="1750000000", allow_changes=True):
        def handle(request):
            self.requests.append(request)
            params = dict(request.params)
            if "modifiedSince" in params and ("startDate" in params or "endDate" in params):
                # exactly what api.teamup.com answers: a date range cannot be
                # combined with a change filter
                return json_response({"error": {"id": "bad_request", "title": "Invalid request", "message": ""}}, status=400)
            if "modifiedSince" in params:
                if not allow_changes:
                    return json_response({"error": {"id": "bad_request"}}, status=400)
                return json_response({"events": list(deleted), "timestamp": int(timestamp)})
            return json_response({"events": list(events), "pages": {"next": None}})

        return handle

    def _client(self, handler) -> TeamupClient:
        return TeamupClient(ig.RecordingTransport(handler), calendar_key=SECRET_ONE, calendar_id=INTERNAL_ID)

    def test_the_window_never_carries_the_change_filter(self):
        seen: list[dict[str, Any]] = []

        def handle(request):
            seen.append(dict(request.params))
            return json_response({"events": [], "pages": {"next": None}})

        self._client(handle).fetch_window(start_date=WINDOW[0], end_date=WINDOW[1])
        self.assertTrue(seen)
        for params in seen:
            self.assertNotIn("modifiedSince", params)
            self.assertIn("startDate", params)
            self.assertIn("endDate", params)

    def test_a_repeat_run_inside_the_window_succeeds_and_reads_the_changelog(self):
        handler = self._handler([ig.teamup_event("occ_1", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00")])
        client = self._client(handler)
        first = ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        self.assertEqual("ok", first.sources["teamup"].status)
        self.assertEqual(1, len(repo.load_occurrences(self.conn)))
        self.assertEqual("not_queried", first.sources["teamup"].payload["changelog"])

        second = ig.run_once(self.conn, [ig.teamup_adapter(client)], now=ig.NOW + timedelta(hours=24), lease=self.lease)
        self.assertEqual("ok", second.sources["teamup"].status)  # used to be HTTP 400
        self.assertEqual("queried", second.sources["teamup"].payload["changelog"])
        self.assertEqual(1, len(repo.load_occurrences(self.conn)))

    def test_an_explicit_deletion_is_read_from_the_changelog(self):
        handler = self._handler([ig.teamup_event("occ_1", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00")])
        ig.run_once(self.conn, [ig.teamup_adapter(self._client(handler))], lease=self.lease)
        self.assertTrue(all(not occurrence.deleted for occurrence in repo.load_occurrences(self.conn)))

        # the next window no longer contains the record; the changelog carries the tombstone
        later = self._client(
            self._handler([], deleted=[ig.teamup_event("occ_1", "2025-07-03T18:00:00+03:00", None, delete=True)])
        )
        report = ig.run_once(self.conn, [ig.teamup_adapter(later)], now=ig.NOW + timedelta(hours=24), lease=self.lease)
        self.assertEqual("ok", report.sources["teamup"].status)
        self.assertEqual(1, report.sources["teamup"].payload["deleted_from_source"])
        occurrences = repo.load_occurrences(self.conn)
        self.assertEqual(1, len(occurrences))
        self.assertTrue(occurrences[0].deleted)
        selection = select_completed(occurrences, mapping=fx.event_mapping(), cutoff=ig.NOW)
        self.assertEqual(0, event_stats(selection, cutoff=ig.NOW).total)

    def test_a_cursor_older_than_the_changelog_is_never_sent(self):
        handler = self._handler([ig.teamup_event("occ_1", "2025-07-03T18:00:00+03:00", "2025-07-03T20:00:00+03:00")])
        client = self._client(handler)
        ig.run_once(self.conn, [ig.teamup_adapter(client)], lease=self.lease)
        self.requests.clear()
        # pretend the cycle was down for 40 days: the changelog cannot cover it
        report = ig.run_once(self.conn, [ig.teamup_adapter(client)], now=ig.NOW + timedelta(days=40), lease=self.lease)
        self.assertEqual("ok", report.sources["teamup"].status)
        self.assertEqual("cursor_older_than_the_changelog", report.sources["teamup"].payload["changelog"])
        self.assertFalse([request for request in self.requests if "modifiedSince" in request.params])

    def test_the_client_refuses_a_cursor_the_changelog_cannot_serve(self):
        def handle(request):  # pragma: no cover - must not be reached
            return json_response({"events": []})

        client = self._client(handle)
        now = int(ig.NOW.timestamp())
        client.fetch_deleted_since(modified_since=now - 3600, now=now)  # inside the 30 days
        with self.assertRaises(ValueError):
            client.fetch_deleted_since(modified_since=now - 40 * 24 * 3600, now=now)
        with self.assertRaises(ValueError):
            client.fetch_deleted_since(modified_since=-1, now=now)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
