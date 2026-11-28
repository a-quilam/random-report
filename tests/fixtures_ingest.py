"""Synthetic fixtures for the ingestion/ops tests.

Everything here is invented: no real account, calendar key, bot token, post or
payload. Tests never reach the network; they use an offline transport.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from randreport.connectors.telegram import RawPost, StaticPostSource, TelegramClient  # noqa: E402
from randreport.connectors.teamup import TeamupClient  # noqa: E402
from randreport.connectors.transport import FakeTransport, Request, Response, TransportError, json_response  # noqa: E402
from randreport.connectors.zenmoney import ZenmoneyClient  # noqa: E402
from randreport.domain.timeframes import MSK, month_period  # noqa: E402
from randreport.news.extract import NewsExtractor  # noqa: E402
from randreport.ops.pipeline import ReleaseBuilder  # noqa: E402
from randreport.ops.run import (  # noqa: E402
    NewsAdapter,
    NightlyRunner,
    TelegramAdapter,
    TeamupAdapter,
    ZenmoneyAdapter,
)
from randreport.store import apply_migrations, connect  # noqa: E402
from tests import fixtures as fx  # noqa: E402

NOW = datetime(2025, 7, 31, 3, 0, tzinfo=MSK)
LOGICAL_DATE = "2025-07-31"
PERIOD = "2025-07"


class Routing:
    """A swappable handler so a test can change the next response."""

    def __init__(self, handler: Callable[[Request], Response]) -> None:
        self.handler = handler
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        return self.handler(request)


class RecordingTransport:
    """Offline transport that routes to a swappable handler and can be made to fail."""

    def __init__(self, handler: Callable[[Request], Response] | None = None) -> None:
        self.handler = handler or (lambda request: json_response({}))
        self.requests: list[Request] = []
        self.error: Exception | None = None

    def send(self, request: Request) -> Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.handler(request)


def fresh_conn(*, check_same_thread: bool = True):
    """A migrated in-memory database.

    ``check_same_thread=False`` is for a connection handed to a WSGI application:
    the server answers each request in a worker thread, and SQLite refuses a
    handle that crosses threads.
    """

    conn = connect(check_same_thread=check_same_thread)
    apply_migrations(conn)
    return conn


# --- Zenmoney -------------------------------------------------------------- #

def zen_row(
    transaction_id: str,
    on: str,
    amount: int,
    *,
    account: str = "acc_card",
    direction: str = "out",
    tag: Sequence[str] | None = None,
    deleted: bool = False,
    income_instrument: int | None = None,
    outcome_instrument: int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"id": transaction_id, "date": f"{on}T12:00:00+03:00", "income": 0, "outcome": 0, "tag": list(tag or [])}
    if deleted:
        row["deleted"] = True
        return row
    if direction == "out":
        row["outcome"] = amount / 100
        row["outcomeAccount"] = account
        if outcome_instrument is not None:
            row["outcomeInstrument"] = outcome_instrument
    else:
        row["income"] = amount / 100
        row["incomeAccount"] = account
        if income_instrument is not None:
            row["incomeInstrument"] = income_instrument
    return row


def zen_transfer(transaction_id: str, on: str, amount: int, *, source: str, target: str) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "date": f"{on}T12:00:00+03:00",
        "income": amount / 100,
        "outcome": amount / 100,
        "incomeAccount": target,
        "outcomeAccount": source,
        "tag": [],
    }


def zen_payload(rows: Sequence[Mapping[str, Any]], *, server_timestamp: int = 1000) -> dict[str, Any]:
    return {"serverTimestamp": server_timestamp, "transaction": list(rows)}


def zenmoney(*, rows: Sequence[Mapping[str, Any]], server_timestamp: int = 1000, token: str = "tok") -> tuple[ZenmoneyClient, RecordingTransport]:
    transport = RecordingTransport(lambda request: json_response(zen_payload(rows, server_timestamp=server_timestamp)))
    return ZenmoneyClient(transport, token=token), transport


# --- Teamup ---------------------------------------------------------------- #

def teamup_event(
    occurrence_id: str,
    start: str,
    end: str | None,
    *,
    subcalendars: Sequence[str] = ("sub_open",),
    title: str | None = "событие",
    series_id: str | None = None,
    delete: bool = False,
    version: int | str | None = 1,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": occurrence_id,
        "subcalendar_ids": list(subcalendars),
        "start_dt": start,
    }
    if version is not None:
        event["version"] = version
    if end is not None:
        event["end_dt"] = end
    if title is not None:
        event["title"] = title
    if series_id is not None:
        event["series_id"] = series_id
    if delete:
        event["delete_dt"] = start
    return event


def teamup_pages(pages: Sequence[Mapping[str, Any]]) -> Callable[[Request], Response]:
    queue = list(pages)

    def handler(request: Request) -> Response:
        payload = queue.pop(0) if queue else {"events": [], "pages": {"next": None}}
        return json_response(payload)

    return handler


def teamup(
    *,
    pages: Sequence[Mapping[str, Any]],
    calendar_key: str = "cal_test",
    calendar_id: str | None = None,
) -> tuple[TeamupClient, RecordingTransport]:
    transport = RecordingTransport(teamup_pages(pages))
    return TeamupClient(transport, calendar_key=calendar_key, calendar_id=calendar_id), transport


# --- Telegram -------------------------------------------------------------- #

def post(message_id: str, text: str, *, day: int = 12, channel: str = "@random_culture", edited: datetime | None = None, deleted: bool = False) -> RawPost:
    return RawPost(
        message_id=message_id,
        channel_id=channel,
        posted_at=datetime(2025, 7, day, 10, 0, tzinfo=MSK),
        text=text,
        edited_at=edited,
        deleted=deleted,
    )


def telegram(
    *,
    posts: Sequence[RawPost],
    members: int | Exception | Sequence[int | Exception] = 4128,
    channel: str = "@random_culture",
    token: str = "bot-token",
) -> tuple[TelegramClient, RecordingTransport]:
    script = list(members) if isinstance(members, (list, tuple)) else [members]

    def handler(request: Request) -> Response:
        if "getChatMemberCount" in request.url:
            value = script.pop(0) if script else script_default
            if isinstance(value, Exception):
                raise value
            return json_response({"ok": True, "result": value})
        return json_response({"ok": True, "result": []})

    script_default = 4128
    transport = RecordingTransport(handler)
    client = TelegramClient(
        transport,
        bot_token=token,
        history=StaticPostSource(posts),
        allowed_channel_ids=(channel,),
    )
    return client, transport


# --- News ------------------------------------------------------------------ #

class FakeProvider:
    """A scripted provider: each call pops the next scripted answer or raises it."""

    model = "stub-1"

    def __init__(self, script: Sequence[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system: str, prompt: str, timeout_s: float) -> str:
        self.calls.append({"system": system, "prompt": prompt, "timeout_s": timeout_s})
        if not self.script:
            raise AssertionError("provider called more times than scripted")
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def news_item(
    *,
    message_ids: Sequence[str] = ("600",),
    classification: str = "news",
    category: str | None = "ремонт",
    title: str = "Отремонтировали подвал",
    summary: str = "Появились новые стеллажи и свет.",
    event_date: str | None = None,
    confidence: Any = "high",
) -> dict[str, Any]:
    return {
        "source_message_ids": list(message_ids),
        "classification": classification,
        "category": category,
        "title": title,
        "summary": summary,
        "event_date": event_date,
        "confidence": confidence,
        "public_only": True,
    }


def news_response(items: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps({"items": list(items)})


def extractor(conn, *, script: Sequence[Any], channel: str = "random_culture", **kwargs) -> NewsExtractor:
    return NewsExtractor(conn, provider=FakeProvider(script), channel_username=channel, **kwargs)


# --- Assembly helpers ------------------------------------------------------ #

def builder(period: str = PERIOD) -> ReleaseBuilder:
    return ReleaseBuilder(policy=fx.policy(), event_mapping=fx.event_mapping(), period=month_period(period))


def run_once(
    conn,
    adapters,
    *,
    now: datetime = NOW,
    release_builder: ReleaseBuilder | None = None,
    publish_dir: str | None = None,
    lease: str | None = None,
    publish: bool = True,
):
    runner = NightlyRunner(
        conn,
        adapters=list(adapters),
        builder=release_builder or builder(),
        publish_dir=publish_dir,
        lease_path=lease,
        retries=1,
        backoff_s=0.0,
        sleep=lambda _seconds: None,
    )
    return runner.run(now=now, publish=publish)


def zenmoney_adapter(client: ZenmoneyClient) -> ZenmoneyAdapter:
    return ZenmoneyAdapter(client)


def teamup_adapter(client: TeamupClient) -> TeamupAdapter:
    return TeamupAdapter(client)


def telegram_adapter(client: TelegramClient) -> TelegramAdapter:
    return TelegramAdapter(client, chat_id="@random_culture", channel_id="@random_culture")


def news_adapter(ext: NewsExtractor) -> NewsAdapter:
    return NewsAdapter(ext)


__all__ = [
    "NOW",
    "LOGICAL_DATE",
    "PERIOD",
    "Routing",
    "RecordingTransport",
    "fresh_conn",
    "zen_row",
    "zen_transfer",
    "zen_payload",
    "zenmoney",
    "teamup_event",
    "teamup_pages",
    "teamup",
    "post",
    "telegram",
    "FakeProvider",
    "news_item",
    "news_response",
    "extractor",
    "builder",
    "run_once",
    "zenmoney_adapter",
    "teamup_adapter",
    "telegram_adapter",
    "news_adapter",
    "TransportError",
    "FakeTransport",
]
