"""Целевые проверки «Что пересылали» (сценарии §9 ТЗ по пересылкам).

The transport is always a fake here: Telethon is imported lazily inside the real
reader, so the suite never needs it installed and never touches the network.
What the tests pin down is everything around the transport — the window, the
storage, the ranking rules, the public DTO, the rendering and the absence of
secrets anywhere in the answers.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from randreport.connectors import telethon_stats
from randreport.connectors.telegram import RawPost
from randreport.connectors.telethon_stats import (
    FetchReport,
    PostCounters,
    StatsConfigError,
    StatsUnavailable,
    TelethonForwardReader,
)
from randreport.domain.dto import AnnouncementsDTO, ForwardItemDTO
from randreport.domain.timeframes import MSK, msk_day
from randreport.news.models import NewsItem
from randreport.ops.run import TelegramStatsAdapter
from randreport.projection.forwards import (
    build_announcements_section,
    ranked,
    ranking_window,
)
from randreport.projection.privacy import public_json
from randreport.store import apply_migrations, connect
from randreport.store import repository as repo
from randreport.web import approved_home
from randreport.web.approved_home import (
    DEFAULT_RANKING_EMPTY,
    DEFAULT_RANKING_NOTE,
    announcements_view,
)

CUTOFF = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)  # 28.11.2026 15:00 MSK
STAMP = CUTOFF.isoformat()


def _post(message_id: str, *, day: date, hour: int = 12, album: str | None = None, deleted: bool = False, text: str = "Анонс: событие\nподробности") -> RawPost:
    posted = datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)
    return RawPost(
        message_id=message_id,
        channel_id="random_culture",
        posted_at=posted,
        text=text,
        deleted=deleted,
        media_album_id=album,
    )


def _news(news_id: str, message_ids: tuple[str, ...], *, classification: str = "announcement", title: str = "Событие") -> NewsItem:
    return NewsItem(
        news_id=news_id,
        classification=classification,
        category="события",
        title=title,
        summary="описание",
        published_on=date(2026, 11, 1),
        source_urls=tuple(f"https://t.me/random_culture/{mid}" for mid in message_ids),
        source_message_ids=message_ids,
        content_hash="hash-" + news_id,
    )


def _counters(*rows: tuple[str, int | None, int | None]) -> FetchReport:
    return FetchReport(
        counters=tuple(
            PostCounters(message_id=mid, channel_id="random_culture", forwards=fw, views=vw)
            for mid, fw, vw in rows
        ),
        fetched_ids=tuple(row[0] for row in rows),
        skipped_ids=(),
        failed_batches=(),
    )


class FakeReader:
    """The nightly adapter's transport stand-in."""

    def __init__(self, report: FetchReport | None = None, error: Exception | None = None) -> None:
        self.report = report or FetchReport((), (), (), ())
        self.error = error
        self.asked: list[list[str]] = []

    def fetch(self, message_ids):
        self.asked.append([str(mid) for mid in message_ids])
        if self.error is not None:
            raise self.error
        return self.report


class ForwardStatsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        apply_migrations(self.conn)
        self.addCleanup(self.conn.close)

    def seed(self, *posts: RawPost) -> None:
        repo.upsert_posts(self.conn, posts, now=STAMP)

    def seed_news(self, *items: NewsItem) -> None:
        for item in items:
            repo.upsert_news_item(self.conn, item, now=STAMP)

    def adapter(self, reader: FakeReader, *, window_days: int = 30) -> TelegramStatsAdapter:
        return TelegramStatsAdapter(reader, window_days=window_days)

    def sync(self, reader: FakeReader, *, cutoff: datetime = CUTOFF, **kwargs):
        return self.adapter(reader, **kwargs).sync(
            conn=self.conn, cutoff=cutoff, logical_date="2026-11-28", cursor=None
        )


# --------------------------------------------------------------------------- #
# §3 конфигурация и транспорт (секреты)                                        #
# --------------------------------------------------------------------------- #


class ConfigTests(unittest.TestCase):
    def test_missing_variables_name_only_the_names(self) -> None:
        with self.assertRaises(StatsConfigError) as ctx:
            telethon_stats.resolve_config(env={})
        message = str(ctx.exception)
        self.assertIn("TELEGRAM_API_ID", message)
        self.assertIn("TELEGRAM_API_HASH", message)
        self.assertIn("TELEGRAM_STATS_BOT_TOKEN", message)
        # no value could have leaked: there was none, and the message lists names
        self.assertNotIn("=", message.replace("=", "="))  # no key=value pairs

    def test_channel_defaults_to_the_public_channel(self) -> None:
        config = telethon_stats.resolve_config(
            env={"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "h", "TELEGRAM_STATS_BOT_TOKEN": "t"}
        )
        self.assertEqual("random_culture", config.channel_username)

    def test_env_overrides_the_channel_and_strips_the_at(self) -> None:
        config = telethon_stats.resolve_config(
            env={
                "TELEGRAM_API_ID": "123",
                "TELEGRAM_API_HASH": "h",
                "TELEGRAM_STATS_BOT_TOKEN": "t",
                "TELEGRAM_CHANNEL_USERNAME": "@other",
            }
        )
        self.assertEqual("other", config.channel_username)

    def test_non_numeric_api_id_is_refused(self) -> None:
        with self.assertRaises(StatsConfigError):
            telethon_stats.resolve_config(
                env={"TELEGRAM_API_ID": "12x", "TELEGRAM_API_HASH": "h", "TELEGRAM_STATS_BOT_TOKEN": "t"}
            )

    def test_repr_hides_every_value(self) -> None:
        config = telethon_stats.resolve_config(
            env={"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash", "TELEGRAM_STATS_BOT_TOKEN": "token"}
        )
        shown = repr(config)
        self.assertNotIn("hash", shown)
        self.assertNotIn("token", shown)

    def test_real_reader_without_telethon_is_an_explicit_unavailable(self) -> None:
        try:  # the assertion holds only while the dependency is absent
            import telethon  # noqa: F401

            self.skipTest("telethon is installed")
        except ImportError:
            pass
        config = telethon_stats.resolve_config(
            env={"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_STATS_BOT_TOKEN": "t"}
        )
        with self.assertRaises(StatsUnavailable):
            TelethonForwardReader(config).fetch(["1500"])


# --------------------------------------------------------------------------- #
# §6 окно, измерение, хранение                                                 #
# --------------------------------------------------------------------------- #


class WindowTests(unittest.TestCase):
    def test_window_is_thirty_inclusive_moscow_days(self) -> None:
        start, end = ranking_window(date(2026, 11, 28))
        self.assertEqual(date(2026, 10, 30), start)
        self.assertEqual(date(2026, 11, 28), end)

    def test_moscow_boundary_decides_membership(self) -> None:
        conn = connect(":memory:")
        apply_migrations(conn)
        self.addCleanup(conn.close)
        # 29.10 20:30 UTC = 29.10 23:30 MSK → outside; 30.10 20:30 UTC = 30.10 23:30 MSK → inside
        repo.upsert_posts(
            conn,
            (_post("100", day=date(2026, 10, 29), hour=20), _post("101", day=date(2026, 10, 30), hour=20)),
            now=STAMP,
        )
        reader = FakeReader(_counters(("101", 5, 10)))
        TelegramStatsAdapter(reader).sync(conn=conn, cutoff=CUTOFF, logical_date="2026-11-28", cursor=None)
        self.assertEqual([["101"]], reader.asked)


class AdapterSyncTests(ForwardStatsTestCase):
    def test_measured_counters_are_stored_with_the_stamp(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10)))
        outcome = self.sync(FakeReader(_counters(("1500", 61, 2358))))
        self.assertEqual("ok", outcome.status)
        rows = repo.post_counter_rows(self.conn)
        self.assertEqual(1, len(rows))
        self.assertEqual(61, rows[0]["forwards"])
        self.assertEqual(2358, rows[0]["views"])
        self.assertEqual(STAMP, rows[0]["fetched_at"])

    def test_a_measured_zero_is_a_zero_and_unknown_stays_null(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10)), _post("1501", day=date(2026, 11, 11)))
        self.sync(FakeReader(_counters(("1500", 0, None), ("1501", None, 5))))
        rows = {row["message_id"]: row for row in repo.post_counter_rows(self.conn)}
        self.assertEqual(0, rows["1500"]["forwards"])
        self.assertIsNone(rows["1501"]["forwards"])

    def test_rerun_updates_values_without_duplicating_rows(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10)))
        self.sync(FakeReader(_counters(("1500", 61, 2358))))
        self.sync(FakeReader(_counters(("1500", 75, 3000))))
        rows = repo.post_counter_rows(self.conn)
        self.assertEqual(1, len(rows))
        self.assertEqual(75, rows[0]["forwards"])

    def test_unanswered_posts_keep_their_previous_values(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10)), _post("1501", day=date(2026, 11, 11)))
        self.sync(FakeReader(_counters(("1500", 61, 2358), ("1501", 5, 100))))
        # the second pass answers only one post: the other keeps its numbers
        self.sync(FakeReader(_counters(("1500", 70, 2400))))
        rows = {row["message_id"]: row for row in repo.post_counter_rows(self.conn)}
        self.assertEqual(70, rows["1500"]["forwards"])
        self.assertEqual(5, rows["1501"]["forwards"])

    def test_telegram_unavailable_raises_and_changes_nothing(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10)))
        self.sync(FakeReader(_counters(("1500", 61, 2358))))
        with self.assertRaises(StatsUnavailable):
            self.sync(FakeReader(error=StatsUnavailable("telegram mtproto read failed")))
        rows = repo.post_counter_rows(self.conn)
        self.assertEqual(1, len(rows))
        self.assertEqual(61, rows[0]["forwards"])

    def test_flood_wait_reports_partial_but_keeps_what_arrived(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10)), _post("1501", day=date(2026, 11, 11)))
        report = FetchReport(
            counters=(PostCounters("1500", "random_culture", 61, 2358),),
            fetched_ids=("1500",),
            skipped_ids=(),
            failed_batches=(),
            flood_wait_s=30,
        )
        outcome = self.sync(FakeReader(report))
        self.assertEqual("partial", outcome.status)
        self.assertEqual(["flood_wait_30s"], outcome.payload["coverage_reasons"])
        rows = {row["message_id"]: row for row in repo.post_counter_rows(self.conn)}
        self.assertEqual(61, rows["1500"]["forwards"])
        self.assertNotIn("1501", rows)

    def test_deleted_posts_are_not_measured(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10), deleted=True), _post("1501", day=date(2026, 11, 11)))
        self.sync(FakeReader(_counters(("1501", 5, 10))))
        self.assertEqual([["1501"]], self._asked)

    def test_empty_window_is_a_clean_pass(self) -> None:
        outcome = self.sync(FakeReader())
        self.assertEqual("ok", outcome.status)
        self.assertEqual(0, outcome.payload["posts"])

    _asked: list[list[str]]

    def sync(self, reader: FakeReader, **kwargs):  # noqa: D102 - record the ids asked
        def wrap(fetch):
            def inner(ids):
                self._asked.append([str(mid) for mid in ids])
                return fetch(ids)

            return inner

        reader.fetch = wrap(reader.fetch)
        return super().sync(reader, **kwargs)

    def setUp(self) -> None:
        super().setUp()
        self._asked = []


# --------------------------------------------------------------------------- #
# §6 правила рейтинга                                                          #
# --------------------------------------------------------------------------- #


class RankingTests(ForwardStatsTestCase):
    def measure(self, reader_report: FetchReport) -> None:
        self.sync(FakeReader(reader_report))

    def test_only_announcements_rank_and_the_rest_is_excluded(self) -> None:
        self.seed(_post("1600", day=date(2026, 11, 1)), _post("1601", day=date(2026, 11, 2)))
        self.seed_news(
            _news("news_a", ("1600",), classification="announcement"),
            _news("news_b", ("1601",), classification="news"),
        )
        self.measure(_counters(("1600", 15, 2136), ("1601", 99, 5000)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=("telegram_stats",),
        )
        self.assertEqual(1, len(dto.items))
        self.assertEqual("https://t.me/random_culture/1600", dto.items[0].url)

    def test_posts_outside_the_window_do_not_rank_even_when_measured(self) -> None:
        self.seed(_post("1400", day=date(2026, 10, 1)), _post("1600", day=date(2026, 11, 1)))
        self.seed_news(_news("news_a", ("1400", "1600")))
        self.measure(_counters(("1400", 200, 1), ("1600", 3, 1)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(("1600",), tuple(item.url.rsplit("/",1)[-1] for item in dto.items))

    def test_sort_is_forwards_desc_then_fresher_then_id(self) -> None:
        self.seed(
            _post("1600", day=date(2026, 11, 1)),
            _post("1601", day=date(2026, 11, 1)),
            _post("1602", day=date(2026, 11, 1)),
            _post("1603", day=date(2026, 11, 2)),  # same forwards as 1600, fresher → above
        )
        self.seed_news(
            _news("a", ("1600",)), _news("b", ("1601",)), _news("c", ("1602",)), _news("d", ("1603",))
        )
        self.measure(_counters(("1600", 15, 1), ("1601", 51, 1), ("1602", 15, 1), ("1603", 15, 1)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(("1601", "1603", "1600"), tuple(item.url.rsplit("/",1)[-1] for item in dto.items))

    def test_at_most_three_cards(self) -> None:
        ids = tuple(str(1600 + i) for i in range(5))
        self.seed(*[_post(mid, day=date(2026, 11, 1)) for mid in ids])
        self.seed_news(*[_news(f"n{i}", (mid,)) for i, mid in enumerate(ids)])
        self.measure(_counters(*[(mid, 10 + i, 1) for i, mid in enumerate(ids)]))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(3, len(dto.items))
        self.assertEqual(("1604", "1603", "1602"), tuple(item.url.rsplit("/",1)[-1] for item in dto.items))

    def test_one_and_two_items_pass_through(self) -> None:
        self.seed(_post("1600", day=date(2026, 11, 1)))
        self.seed_news(_news("a", ("1600",)))
        self.measure(_counters(("1600", 61, 2358)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(1, len(dto.items))
        self.assertEqual(1, dto.items[0].rank)

    def test_unknown_forwards_excluded_measured_zero_included(self) -> None:
        self.seed(_post("1600", day=date(2026, 11, 1)), _post("1601", day=date(2026, 11, 1)))
        self.seed_news(_news("a", ("1600",)), _news("b", ("1601",)))
        self.measure(_counters(("1600", None, 5), ("1601", 0, 5)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(("1601",), tuple(item.url.rsplit("/",1)[-1] for item in dto.items))
        self.assertEqual(0, dto.items[0].forwards)

    def test_a_media_group_is_one_publication_with_the_main_message_counter(self) -> None:
        self.seed(_post("1700", day=date(2026, 11, 1), album="grp1"), _post("1701", day=date(2026, 11, 1), album="grp1"))
        self.seed_news(_news("a", ("1700", "1701")))
        self.measure(_counters(("1700", 51, 2449), ("1701", 61, 2500)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(1, len(dto.items))
        # the main (first) message's counter stands for the album
        self.assertEqual("https://t.me/random_culture/1700", dto.items[0].url)
        self.assertEqual(51, dto.items[0].forwards)

    def test_two_announcements_of_one_album_collapse_into_one(self) -> None:
        self.seed(_post("1700", day=date(2026, 11, 1), album="grp1"))
        self.seed_news(_news("a", ("1700",)), _news("b", ("1700",)))
        self.measure(_counters(("1700", 51, 2449)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(1, len(dto.items))

    def test_hidden_or_deleted_announcements_do_not_rank(self) -> None:
        self.seed(_post("1600", day=date(2026, 11, 1)), _post("1601", day=date(2026, 11, 1)))
        self.seed_news(_news("a", ("1600",)), _news("b", ("1601",)))
        repo.set_manual_state(self.conn, news_id="a", state="hidden", now=STAMP)
        repo.mark_news_source_deleted(self.conn, news_id="b", now=STAMP)
        self.measure(_counters(("1600", 99, 1), ("1601", 99, 1)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual((), tuple(dto.items))

    def test_links_point_at_the_original_posts(self) -> None:
        self.seed(_post("1559", day=date(2026, 11, 1)))
        self.seed_news(_news("a", ("1559",)))
        self.measure(_counters(("1559", 51, 2449)))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual("https://t.me/random_culture/1559", dto.items[0].url)


# --------------------------------------------------------------------------- #
# §7 публичный DTO и отображение                                               #
# --------------------------------------------------------------------------- #


class DtoTests(unittest.TestCase):
    def _dto(self) -> AnnouncementsDTO:
        return AnnouncementsDTO(
            as_of=date(2026, 11, 28),
            release_id="rel_x",
            freshness="fresh",
            verification="operational",
            completeness="complete",
            source_labels=("telegram_stats",),
            window_from=date(2026, 10, 30),
            window_to=date(2026, 11, 28),
            fetched_at=STAMP,
            items=(
                ForwardItemDTO(rank=1, title="Событие", url="https://t.me/random_culture/1559", forwards=51, published_on=date(2026, 11, 1)),
            ),
        )

    def test_public_json_round_trip(self) -> None:
        dto = self._dto()
        restored = AnnouncementsDTO.from_public_dict(json.loads(public_json(dto)))
        self.assertEqual(dto.items[0].title, restored.items[0].title)
        self.assertEqual(dto.window_from, restored.window_from)

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            AnnouncementsDTO.from_public_dict({**json.loads(public_json(self._dto())), "views_of_everything": 1})

    def test_no_secrets_in_the_public_payload(self) -> None:
        text = public_json(self._dto())
        for marker in ("TELEGRAM_API_HASH", "TELEGRAM_STATS_BOT_TOKEN", "api_hash", "bot_token", "token"):
            self.assertNotIn(marker, text)

    def test_release_index_reads_the_section_back(self) -> None:
        from randreport.web.release_index import ReleaseIndex

        dto = self._dto()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = root / "rel_x"
            rel.mkdir()
            payload = json.loads(public_json(dto))
            (rel / "announcements.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            (rel / "index.json").write_text(
                json.dumps({"release_id": "rel_x", "as_of": STAMP, "sections": ["announcements"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            release = ReleaseIndex(root).get("rel_x")
            self.assertIsNotNone(release.announcements)
            self.assertEqual("https://t.me/random_culture/1559", release.announcements.items[0].url)

    def test_release_without_the_section_keeps_none(self) -> None:
        from randreport.web.release_index import ReleaseIndex

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = root / "rel_old"
            rel.mkdir()
            (rel / "index.json").write_text(
                json.dumps({"release_id": "rel_old", "as_of": STAMP, "sections": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertIsNone(ReleaseIndex(root).get("rel_old").announcements)


class RenderTests(unittest.TestCase):
    def _dto(self, *, items=(), fetched_at=STAMP) -> AnnouncementsDTO:
        return AnnouncementsDTO(
            as_of=date(2026, 11, 28),
            release_id="rel_x",
            freshness="fresh",
            verification="operational",
            completeness="complete",
            source_labels=(),
            window_from=date(2026, 10, 30),
            window_to=date(2026, 11, 28),
            fetched_at=fetched_at,
            items=items,
        )

    def test_subtitle_names_the_explicit_window(self) -> None:
        cards, note, _ = announcements_view(self._dto())
        self.assertEqual("ТОП-3 АНОНСА · 30.10—28.11.2026", note)

    def test_never_measured_state_is_not_a_zero(self) -> None:
        cards, _, empty = announcements_view(self._dto(fetched_at=None))
        self.assertEqual((), cards)
        self.assertIn("ещё ни разу не получалась", empty)

    def test_measured_but_empty_window_says_so(self) -> None:
        _, _, empty = announcements_view(self._dto(items=()))
        self.assertIn("анонсов с измеренными пересылками нет", empty)

    def test_release_without_the_section_keeps_the_generic_texts(self) -> None:
        cards, note, empty = announcements_view(None)
        self.assertEqual((), cards)
        self.assertEqual(DEFAULT_RANKING_NOTE, note)
        self.assertEqual(DEFAULT_RANKING_EMPTY, empty)

    def test_cards_carry_rank_title_and_pluralised_forwards(self) -> None:
        items = (
            ForwardItemDTO(rank=1, title="Одна пересылка", url="https://t.me/random_culture/61", forwards=1, published_on=date(2026, 11, 1)),
            ForwardItemDTO(rank=2, title="Две", url="https://t.me/random_culture/62", forwards=2, published_on=date(2026, 11, 1)),
            ForwardItemDTO(rank=3, title="Одиннадцать", url="https://t.me/random_culture/63", forwards=11, published_on=date(2026, 11, 1)),
        )
        cards, _, _ = announcements_view(self._dto(items=items))
        ranking = approved_home._ranking(cards)
        self.assertIn("№ 01", ranking)
        self.assertIn("↗ 1 пересылка", ranking)
        self.assertIn("↗ 2 пересылки", ranking)
        self.assertIn("↗ 11 пересылок", ranking)

    def test_hostile_title_is_escaped(self) -> None:
        items = (
            ForwardItemDTO(rank=1, title="<script>", url="https://t.me/random_culture/1", forwards=3, published_on=date(2026, 11, 1)),
        )
        cards, _, _ = announcements_view(self._dto(items=items))
        ranking = approved_home._ranking(cards)
        self.assertNotIn("<script>", ranking)
        self.assertIn("&lt;script&gt;", ranking)

    def test_unsafe_url_never_becomes_a_card(self) -> None:
        items = (
            ForwardItemDTO(rank=1, title="x", url="javascript:alert(1)", forwards=3, published_on=date(2026, 11, 1)),
        )
        cards, _, _ = announcements_view(self._dto(items=items))
        self.assertEqual(1, len(cards))
        self.assertEqual(
            f'<p class="financial-note">{DEFAULT_RANKING_EMPTY}</p>',
            approved_home._ranking(cards),
        )

    def test_page_prints_the_note_and_the_cards(self) -> None:
        from randreport.web.approved_home import page

        html = page(
            period_label="2026-11",
            money=None,
            events=None,
            community=None,
            historical={},
            announcements=(
                {"title": "Событие", "url": "https://t.me/random_culture/1559", "forwards": 51},
            ),
            ranking_note="ТОП-3 АНОНСА · 30.10—28.11.2026",
            ranking_empty=DEFAULT_RANKING_EMPTY,
        )
        self.assertIn("ТОП-3 АНОНСА · 30.10—28.11.2026", html)
        self.assertIn("https://t.me/random_culture/1559", html)
        self.assertIn("↗ 51 пересыл", html)

    def test_page_without_announcements_prints_the_honest_line(self) -> None:
        from randreport.web.approved_home import page

        html = page(
            period_label="2026-11",
            money=None,
            events=None,
            community=None,
            historical={},
            ranking_empty="Статистика пересылок канала ещё ни разу не получалась.",
        )
        self.assertIn("Статистика пересылок канала ещё ни разу не получалась.", html)


# --------------------------------------------------------------------------- #
# §8 журналы и отсутствие секретов в ответах приложения                        #
# --------------------------------------------------------------------------- #


class AdapterPayloadTests(ForwardStatsTestCase):
    def test_payload_counts_only_no_values_and_no_secrets(self) -> None:
        self.seed(_post("1500", day=date(2026, 11, 10)))
        outcome = self.sync(FakeReader(_counters(("1500", 61, 2358))))
        text = json.dumps(outcome.payload, ensure_ascii=False)
        self.assertNotIn('"forwards"', text)
        self.assertNotIn('"views"', text)
        self.assertNotIn("token", text.lower())
        self.assertNotIn("api_hash", text.lower())
        for key in ("posts", "measured", "updated", "window_from", "window_to", "latest_fetch"):
            self.assertIn(key, outcome.payload)

    def test_section_window_covers_the_thirty_days_before_the_cutoff(self) -> None:
        self.seed(_post("1600", day=date(2026, 11, 1)))
        self.seed_news(_news("a", ("1600",)))
        self.sync(FakeReader(_counters(("1600", 61, 2358))))
        dto = build_announcements_section(
            self.conn, cutoff=CUTOFF, freshness="fresh", verification="operational",
            completeness="complete", source_labels=(),
        )
        self.assertEqual(date(2026, 10, 30), dto.window_from)
        self.assertEqual(date(2026, 11, 28), dto.window_to)
        self.assertEqual(1, len(dto.items))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
