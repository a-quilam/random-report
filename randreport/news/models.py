"""News models and the strict validator for model output."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Mapping, Sequence

from ..connectors.telegram import RawPost
from ..domain.timeframes import MSK


class Classification(StrEnum):
    NEWS = "news"
    ANNOUNCEMENT = "announcement"
    OTHER = "other"


ALLOWED_FIELDS = frozenset(
    {
        "source_message_ids",
        "classification",
        "category",
        "title",
        "summary",
        "event_date",
        "confidence",
        "public_only",
    }
)

MAX_TITLE = 200
MAX_SUMMARY = 2000


class ExtractionInvalid(ValueError):
    """Model output that must not reach the public chronicle."""


@dataclass(frozen=True)
class Extraction:
    source_message_ids: tuple[str, ...]
    classification: Classification
    category: str | None
    title: str
    summary: str
    event_date: date | None = None
    confidence: str | None = None
    public_only: bool = True


def _require_str(value: Any, field_name: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionInvalid(f"{field_name} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise ExtractionInvalid(f"{field_name} is longer than {limit} characters")
    return text


def _parse_confidence(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ExtractionInvalid("confidence must be a string or an integer, not a float")
    if isinstance(value, int):
        if not 0 <= value <= 100:
            raise ExtractionInvalid("integer confidence must be between 0 and 100")
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ExtractionInvalid("confidence must be a string or an integer")


def _parse_event_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ExtractionInvalid("event_date must be an ISO date string or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ExtractionInvalid("event_date is not an ISO date") from exc


def parse_extraction(raw: Any) -> Extraction:
    """Validate one extraction object; reject anything outside the strict schema."""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ExtractionInvalid("model output is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise ExtractionInvalid("model output must be a JSON object")

    unknown = sorted(set(raw) - ALLOWED_FIELDS)
    if unknown:
        raise ExtractionInvalid(f"fields outside the schema: {unknown}")
    missing = sorted(ALLOWED_FIELDS - set(raw))
    if missing:
        raise ExtractionInvalid(f"missing required fields: {missing}")

    if raw["public_only"] is not True:
        raise ExtractionInvalid("public_only must be true")

    classification_raw = raw["classification"]
    try:
        classification = Classification(classification_raw)
    except ValueError as exc:
        raise ExtractionInvalid(f"unknown classification {classification_raw!r}") from exc

    ids = raw["source_message_ids"]
    if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)) or not ids:
        raise ExtractionInvalid("source_message_ids must be a non-empty list")
    if not all(isinstance(item, str) and item for item in ids):
        raise ExtractionInvalid("source_message_ids must contain strings")

    category = raw["category"]
    if category is not None and (not isinstance(category, str) or not category.strip()):
        raise ExtractionInvalid("category must be a non-empty string or null")

    return Extraction(
        source_message_ids=tuple(ids),
        classification=classification,
        category=None if category is None else category.strip(),
        title=_require_str(raw["title"], "title", limit=MAX_TITLE),
        summary=_require_str(raw["summary"], "summary", limit=MAX_SUMMARY),
        event_date=_parse_event_date(raw["event_date"]),
        confidence=_parse_confidence(raw["confidence"]),
        public_only=True,
    )


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewsItem:
    news_id: str
    classification: str
    category: str | None
    title: str
    summary: str
    published_on: date
    source_urls: tuple[str, ...]
    source_message_ids: tuple[str, ...]
    content_hash: str
    origin: str = "llm"
    prompt_version: str | None = None
    model: str | None = None
    machine_processed: bool = True
    verified: bool = False
    public_only: bool = True
    manual_state: str = "active"
    event_date: date | None = None
    confidence: str | None = None


def build_news_item(
    extraction: Extraction,
    *,
    posts: Mapping[str, RawPost],
    channel_username: str,
    prompt_version: str,
    model: str,
) -> NewsItem:
    """Turn a validated extraction into a stored item.

    Links are constructed here from the archived message ids; the model never
    supplies a URL. The id is content-addressed on the source messages, so a
    repeated run cannot duplicate a news item.
    """

    missing = [mid for mid in extraction.source_message_ids if mid not in posts]
    if missing:
        raise ExtractionInvalid(f"extraction cites messages that were not sent to the model: {missing}")

    source_urls = tuple(
        f"https://t.me/{channel_username}/{mid}" for mid in sorted(extraction.source_message_ids)
    )
    published_days = [posts[mid].posted_at.astimezone(MSK).date() for mid in extraction.source_message_ids]
    published_on = max(published_days)
    news_id = "news_" + _digest("|".join(sorted(extraction.source_message_ids)), prompt_version)[:16]
    content_hash = _digest(
        str(extraction.classification),
        extraction.category or "",
        extraction.title,
        extraction.summary,
        "" if extraction.event_date is None else extraction.event_date.isoformat(),
        "|".join(sorted(extraction.source_message_ids)),
    )
    return NewsItem(
        news_id=news_id,
        classification=str(extraction.classification),
        category=extraction.category,
        title=extraction.title,
        summary=extraction.summary,
        published_on=published_on,
        source_urls=source_urls,
        source_message_ids=tuple(sorted(extraction.source_message_ids)),
        content_hash=content_hash,
        origin="llm",
        prompt_version=prompt_version,
        model=model,
        event_date=extraction.event_date,
        confidence=extraction.confidence,
    )
