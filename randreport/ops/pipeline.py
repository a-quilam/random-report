"""Build one public release out of the persisted facts, through the domain layer.

The builder only reads the private store, calls the frozen domain functions and
emits leak-checked public DTOs. It never queries a source and never derives money
or events on its own. The release id is content-addressed over the published data,
so an unchanged day yields the same release and a changed one a new id.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..domain.dto import (
    ChannelPointDTO,
    ChronicleDTO,
    CommunityDTO,
    MoneyDTO,
    NewsItemDTO,
    StrictDTO,
)
from ..domain.allocation import apply_allocation
from ..domain.events import EventTypeMapping, event_stats, select_completed
from ..domain.finance import (
    Liability,
    ScopePolicy,
    build_ledger,
    compute_totals,
    consolidated_balance,
    reconcile_totals,
)
from ..domain.timeframes import Period, msk_day
from ..projection.privacy import public_json
from ..projection.release import (
    ReleaseContext,
    build_events_payload,
    build_money_payload,
    release_id_for,
)
from ..store import repository as repo
from .publish import publish_release
from .schedule import utc_now

RELEASE_PLACEHOLDER = "rel_pending"
NEWS_CHANNEL_USERNAME = "random_culture"


@dataclass(frozen=True)
class PublicRelease:
    release_id: str
    as_of: datetime
    sections: Mapping[str, StrictDTO]
    payload_json: Mapping[str, str]
    checks: tuple[Mapping[str, Any], ...]

    def combined_payload(self) -> str:
        return json.dumps(
            {name: json.loads(text) for name, text in sorted(self.payload_json.items())},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def files(self) -> dict[str, str]:
        files = {f"{name}.json": text for name, text in self.payload_json.items()}
        files["index.json"] = json.dumps(
            {"release_id": self.release_id, "as_of": self.as_of.isoformat(), "sections": sorted(self.payload_json)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return files


def _strip_release_id(node: Any) -> Any:
    if isinstance(node, Mapping):
        return {key: _strip_release_id(value) for key, value in node.items() if key != "release_id"}
    if isinstance(node, (list, tuple)):
        return [_strip_release_id(item) for item in node]
    return node


def content_release_id(sections: Mapping[str, StrictDTO]) -> str:
    """Content-addressed release id over the published data (excluding its own id)."""

    canonical = {name: _strip_release_id(dto.to_public_dict()) for name, dto in sections.items()}
    return release_id_for(canonical)


class ReleaseBuilder:
    """Turns persisted facts into the public sections of one release."""

    def __init__(
        self,
        *,
        policy: ScopePolicy,
        event_mapping: EventTypeMapping,
        period: Period,
        rates: Mapping[str, Decimal] | None = None,
        liabilities: Sequence[Liability] = (),
        schema_version: str = "public-release-1",
    ) -> None:
        self.policy = policy
        self.event_mapping = event_mapping
        self.period = period
        self.rates = dict(rates or {})
        self.liabilities = tuple(liabilities)
        self.schema_version = schema_version

    def build(
        self,
        conn: sqlite3.Connection,
        *,
        cutoff: datetime,
        freshness: str = "fresh",
        verification: str = "operational",
        completeness: str = "complete",
        source_labels: Sequence[str] = (),
        finance_data_present: bool = True,
    ) -> PublicRelease:
        cutoff_day: date = msk_day(cutoff)

        movements = repo.load_movements(conn)
        ledger = build_ledger(movements, self.policy, self.period, source_data_present=finance_data_present)
        # the owner's confirmed overrides are folded in before the totals, so
        # every aggregate, CSV row and HTML row is derived from one ledger
        ledger, allocation_report = apply_allocation(ledger, self.policy.allocation, policy=self.policy)
        totals = compute_totals(ledger, as_of=cutoff_day)
        balance = consolidated_balance(
            repo.load_balances(conn),
            as_of=None,
            rates=self.rates,
            liabilities=self.liabilities,
            scope_policy=self.policy,
        )
        checks = list(reconcile_totals(ledger, totals))

        selection = select_completed(
            # The period of a release decides which occurrences it reports: the
            # calendar section of a monthly or yearly release must describe that
            # period (docs/REQUIREMENTS.md section 3.3), not the whole 400-day
            # window. An occurrence belongs to the period of its Moscow start day;
            # an ``all_time`` release keeps everything.
            (
                occurrence
                for occurrence in repo.load_occurrences(conn)
                if self.period.contains(occurrence.day())
            ),
            mapping=self.event_mapping,
            cutoff=cutoff,
        )
        stats = event_stats(selection, cutoff=cutoff)

        ctx = ReleaseContext(
            release_id=RELEASE_PLACEHOLDER,
            as_of=cutoff,
            period=self.period,
            freshness=freshness,
            verification=verification,
            completeness=completeness,
            source_labels=tuple(source_labels),
        )

        money = build_money_payload(ctx, totals, balance=balance, allocation=allocation_report)
        events = build_events_payload(ctx, selection, stats=stats)
        community = self._community(ctx, conn)
        chronicle = self._chronicle(ctx, conn)

        sections: dict[str, StrictDTO] = {
            "money": money,
            "events": events,
            "community": community,
            "chronicle": chronicle,
        }
        release_id = content_release_id(sections)
        final = {name: dataclasses.replace(dto, release_id=release_id) for name, dto in sections.items()}  # type: ignore[type-var]
        payload_json = {name: public_json(dto) for name, dto in final.items()}

        return PublicRelease(
            release_id=release_id,
            as_of=cutoff,
            sections=final,
            payload_json=payload_json,
            checks=tuple(check.__dict__ for check in checks),
        )

    # --- sections ---------------------------------------------------------- #

    @staticmethod
    def _community(ctx: ReleaseContext, conn: sqlite3.Connection) -> CommunityDTO:
        points = repo.channel_points(conn)
        inside = [(day, value, source) for day, value, source in points if ctx.period.contains(day)]
        usable = inside if inside else []
        dtos = tuple(ChannelPointDTO(on=day, subscribers=value, source=source) for day, value, source in usable)
        metrics: dict[str, Any] = {
            "points": len(dtos),
            "latest_subscribers": dtos[-1].subscribers if dtos else None,
            "latest_on": dtos[-1].on.isoformat() if dtos else None,
            "change": None,
            "change_from": None,
            "change_to": None,
            "interpolated": False,
            "has_gap": False,
        }
        if len(dtos) >= 2:
            metrics["change"] = dtos[-1].subscribers - dtos[0].subscribers
            metrics["change_from"] = dtos[0].on.isoformat()
            metrics["change_to"] = dtos[-1].on.isoformat()
        metrics["has_gap"] = len(dtos) < max(1, len(ctx.period.months())) and len(dtos) > 0
        return CommunityDTO(
            period=ctx.period_dto(),
            as_of=ctx.as_of_day(),
            release_id=ctx.release_id,
            freshness=ctx.freshness,
            verification=ctx.verification,
            completeness=ctx.completeness,
            source_labels=tuple(ctx.source_labels),
            channel_points=dtos,
            metrics=metrics,
        )

    @staticmethod
    def _chronicle(ctx: ReleaseContext, conn: sqlite3.Connection) -> ChronicleDTO:
        rows = repo.public_news_items(conn)
        dtos = tuple(
            NewsItemDTO(
                id=row["news_id"],
                title=row["title"] or "",
                summary=row["summary"] or "",
                category=row["category"] or "прочее",
                published_on=date.fromisoformat(row["published_on"]),
                source_urls=_urls(row["source_message_ids"]),
                machine_processed=bool(row["machine_processed"]),
                verified=bool(row["verified"]),
            )
            for row in rows
        )
        within = tuple(dto for dto in dtos if ctx.period.contains(dto.published_on)) if ctx.period.kind != "all_time" else dtos
        metrics = {
            "news_count": len(within),
            "machine_processed": True,
            "announcements_excluded": True,
        }
        return ChronicleDTO(
            period=ctx.period_dto(),
            as_of=ctx.as_of_day(),
            release_id=ctx.release_id,
            freshness=ctx.freshness,
            verification=ctx.verification,
            completeness=ctx.completeness,
            source_labels=tuple(ctx.source_labels),
            news_items=tuple(within),
            metrics=metrics,
        )


def _urls(source_message_ids: str) -> tuple[str, ...]:
    """Rebuild the public t.me links from archived message ids (never from the model)."""

    message_ids = json.loads(source_message_ids)
    return tuple(f"https://t.me/{NEWS_CHANNEL_USERNAME}/{mid}" for mid in sorted(message_ids))


def build_releases(
    conn: sqlite3.Connection,
    *,
    policy: ScopePolicy,
    event_mapping: EventTypeMapping,
    periods: Sequence[Period],
    release_dir: str | Path | None = None,
    now: datetime | None = None,
    freshness: str = "fresh",
    verification: str = "operational",
    completeness: str = "complete",
    source_labels: Sequence[str] = (),
    finance_data_present: bool = True,
    rates: Mapping[str, Decimal] | None = None,
    liabilities: Sequence[Liability] = (),
) -> list[tuple[str, str]]:
    """Build and store one release per period, atomically publishing each one.

    Every period of the list gets its own content-addressed release, so a site can
    serve all available whole periods *and* the current, still unfinished one from
    the same confirmed map.  ``freshness``/``verification`` describe the run, not
    the period.  Returns ``(period_label, release_id)`` in the given order.
    """

    stamp = now or utc_now()
    built: list[tuple[str, str]] = []
    for period in periods:
        builder = ReleaseBuilder(
            policy=policy,
            event_mapping=event_mapping,
            period=period,
            rates=rates,
            liabilities=liabilities,
        )
        release = builder.build(
            conn,
            cutoff=stamp,
            freshness=freshness,
            verification=verification,
            completeness=completeness,
            source_labels=tuple(source_labels),
            finance_data_present=finance_data_present,
        )
        repo.store_release(
            conn,
            release_id=release.release_id,
            as_of=release.as_of.isoformat(),
            schema_version=builder.schema_version,
            sections=tuple(release.payload_json),
            payload_text=release.combined_payload(),
            built_at=stamp.isoformat(),
            make_current=True,
        )
        if release_dir is not None:
            # the allowlisted report archive travels inside the release, so the
            # public site can render /reports/* without the private database
            from ..web.build import release_files
            from .publish import prune_superseded_releases

            publish_release(release_dir, release.release_id, release_files(release, conn))
            # a rebuild of a period (after a code or map change) creates a new
            # content-addressed directory; the superseded one is removed so the
            # stale projection can never be served (private-store revisions and
            # the /reports/* archive are untouched — this is only the public dir)
            prune_superseded_releases(release_dir, period_label=period.label, keep_id=release.release_id)
        built.append((period.label, release.release_id))
    return built
