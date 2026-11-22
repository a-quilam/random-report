"""The version archive of period reports (docs/REQUIREMENTS.md section 3.7, R01).

A period report has two layers:

* the **published snapshot** — the original report of a past period, tied to the
  public post it came from. It is version 1 and immutable: it is never rewritten,
  and it is the only place a historical number is treated as fixed;
* the **recomputed / corrected layer** — an operational version recalculated from
  reliable facts, and any later correction of it. Every change arrives as a *new*
  version with a diff and a reason; the previous versions stay reachable at their
  permanent URLs.

The public page therefore shows what changed and why, without ever exposing a
transaction. Nothing in this module invents a fact: the generated report text is
built only from stored aggregates, and :func:`assert_text_faithful` proves it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .. import history
from ..store import repository as repo

PUBLISHED = "published"
RECOMPUTED = "recomputed"
CORRECTED = "corrected"

VERSION_SOURCES = (PUBLISHED, RECOMPUTED, CORRECTED)

FIVE_SECTIONS = ("Поступления", "Расходы", "Мероприятия", "Сообщество", "Итог")

SNAPSHOT_FIELDS = ("events", "receipts", "expenses")


class ReportError(ValueError):
    """A report snapshot request that cannot be honoured."""


@dataclass(frozen=True)
class Correction:
    version: int
    at: str
    actor: str | None
    reason: str
    changes: Mapping[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_rub(value: int | None) -> str:
    if value is None:
        return "нет данных"
    rubles = Decimal(value)
    text = f"{rubles:,.0f}".replace(",", " ")
    return f"{text} ₽"


def _coverage_text(receipts: int | None, expenses: int | None) -> str:
    percent = history.coverage_percent(receipts, expenses)
    if percent is None:
        return "не применимо"
    return f"{percent} %"


def snapshot_payload(report: history.HistoricalReport) -> dict[str, Any]:
    """The structured snapshot of one published report (aggregates only)."""

    return {
        "period": report.label,
        "period_from": report.start,
        "period_to": report.end,
        "source_report_id": f"tg:{history.CHANNEL_USERNAME}:{report.post}",
        "source_url": report.url,
        "published_on": report.published,
        "events": report.events,
        "receipts": report.receipts,
        "expenses": report.expenses,
        "unit": "RUB",
        "provenance": PUBLISHED,
        "coverage": history.COMPLETE,
        "original_text": None,
        "note": history.PARAPHRASE_NOTE,
    }


def seed_published_snapshots(conn, *, published_at: str | None = None) -> int:
    """Ensure version 1 of every published report exists. Idempotent.

    The original text is deliberately ``None``: only the paraphrase survived, and
    a paraphrase is not a faithful copy of the original figures.
    """

    stamp = published_at or _now()
    written = 0
    for report in history.published_reports():
        payload = snapshot_payload(report)
        outcome = repo.record_report_snapshot(
            conn,
            report_id=f"report:{report.label}",
            period_label=report.label,
            version=1,
            published_at=report.published,
            payload=payload,
            original_text=None,
            original_url=report.url,
        )
        if outcome == "inserted":
            written += 1
    return written


def _diff(previous: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for field, new_value in changes.items():
        old_value = previous.get(field)
        if old_value != new_value:
            out[field] = {"from": old_value, "to": new_value}
    return out


def record_correction(
    conn,
    *,
    period_label: str,
    changes: Mapping[str, Any],
    reason: str,
    actor: str | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    """Record a corrected version of a period report; the previous versions stay.

    ``changes`` maps an aggregate field to its new value. The original snapshot is
    never touched and a fix-up row is never invented: the correction is a visible
    new version with its diff and its reason.
    """

    if not reason:
        raise ReportError("a correction needs a reason")
    unknown = sorted(set(changes) - set(SNAPSHOT_FIELDS))
    if unknown:
        raise ReportError(f"a correction may only change {SNAPSHOT_FIELDS}, not {unknown}")
    latest = repo.latest_report_snapshot(conn, period_label)
    if latest is None:
        raise ReportError(f"no published snapshot for period {period_label!r}")
    previous = json.loads(latest["payload"])
    version = int(latest["version"]) + 1
    diff = _diff(previous, changes)
    if not diff:
        raise ReportError("a correction must change at least one field")
    updated = dict(previous)
    for field, change in diff.items():
        updated[field] = change["to"]
    updated["provenance"] = CORRECTED
    updated["coverage"] = latest_coverage(previous)
    corrections = list(previous.get("corrections", []))
    corrections.append(
        {"version": version, "at": published_at or _now(), "actor": actor, "reason": reason, "changes": diff}
    )
    updated["corrections"] = corrections
    repo.record_report_snapshot(
        conn,
        report_id=f"report:{period_label}:v{version}",
        period_label=period_label,
        version=version,
        published_at=published_at or _now(),
        payload=updated,
        original_text=None,
        original_url=previous.get("source_url"),
    )
    return {"version": version, "diff": diff, "payload": updated}


def latest_coverage(payload: Mapping[str, Any]) -> str:
    return str(payload.get("coverage", history.COMPLETE))


def versions(conn, period_label: str) -> tuple[dict[str, Any], ...]:
    return repo.report_versions(conn, period_label)


def public_view(conn, period_label: str) -> dict[str, Any]:
    """Everything the public report page may show for one period."""

    rows = versions(conn, period_label)
    if not rows:
        return {"period": period_label, "versions": (), "latest": None, "original": None, "corrections": ()}
    original = json.loads(rows[0]["payload"])
    latest = json.loads(rows[-1]["payload"])
    corrections = tuple(
        Correction(
            version=int(c["version"]),
            at=str(c.get("at", "")),
            actor=c.get("actor"),
            reason=str(c.get("reason", "")),
            changes=dict(c.get("changes", {})),
        )
        for c in latest.get("corrections", [])
    )
    summaries = tuple(
        {
            "version": int(row["version"]),
            "published_at": row["published_at"],
            "provenance": json.loads(row["payload"]).get("provenance", PUBLISHED),
            "url": f"/reports/{period_label}/v{int(row['version'])}",
        }
        for row in rows
    )
    return {
        "period": period_label,
        "versions": summaries,
        "latest": latest,
        "original": original,
        "corrections": corrections,
        "changed": len(rows) > 1,
    }


def version_payload(conn, period_label: str, version: int) -> dict[str, Any] | None:
    for row in versions(conn, period_label):
        if int(row["version"]) == version:
            return json.loads(row["payload"])
    return None


# --------------------------------------------------------------------------- #
# Report text generation (must not invent facts)
# --------------------------------------------------------------------------- #

def report_text(payload: Mapping[str, Any]) -> str:
    """A plain-text draft of the traditional five-section monthly report.

    Every figure comes from the snapshot; the function holds no constants other
    than the section headings. :func:`assert_text_faithful` enforces that.
    """

    period = str(payload.get("period", ""))
    lines = [f"Отчёт за {period}"]
    for heading in FIVE_SECTIONS:
        lines.append(f"\n{heading}")
        if heading == "Поступления":
            lines.append(f"Поступления: {_fmt_rub(payload.get('receipts'))}")
        elif heading == "Расходы":
            lines.append(f"Расходы: {_fmt_rub(payload.get('expenses'))}")
        elif heading == "Мероприятия":
            events = payload.get("events")
            lines.append(f"Состоялось мероприятий: {events if events is not None else 'нет данных'}")
        elif heading == "Сообщество":
            lines.append("Данных о подписчиках за период в этом снимке нет")
        else:
            receipts, expenses = payload.get("receipts"), payload.get("expenses")
            if receipts is None or expenses is None:
                lines.append("Итог: нет данных для разницы поступлений и расходов")
            else:
                lines.append(
                    f"Разница (поступления − расходы): {_fmt_rub(int(receipts) - int(expenses))}, "
                    f"покрытие {_coverage_text(receipts, expenses)}"
                )
    note = payload.get("note")
    if note:
        lines.append(f"\n{note}")
    if payload.get("source_url"):
        lines.append(f"Источник: {payload['source_url']}")
    lines.append(f"\n{history.NO_BALANCE_NOTE}")
    return "\n".join(lines)


_NUMBER_RE = __import__("re").compile(r"-?\d[\d \u00a0]*(?:[.,]\d+)?")


def _norm_num(text: str) -> str:
    return text.replace("\u00a0", "").replace(" ", "")


def _numbers(value: Any) -> set[str]:
    """Every numeric token a snapshot legitimately contains (normalised)."""

    out: set[str] = set()
    if isinstance(value, bool) or value is None:
        return out
    if isinstance(value, (int, float, Decimal)):
        out.add(_norm_num(str(value)))
        return out
    if isinstance(value, str):
        for token in _NUMBER_RE.findall(value):
            out.add(_norm_num(token))
        return out
    if isinstance(value, Mapping):
        for item in value.values():
            out |= _numbers(item)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            out |= _numbers(item)
    return out


def allowed_numbers(payload: Mapping[str, Any]) -> set[str]:
    """Numbers a faithful report text may state: the stored aggregates, their
    difference and their coverage ratio, plus any number already in the snapshot.
    Nothing else is derivable, so nothing else is allowed.
    """

    allowed = _numbers(payload)
    receipts, expenses = payload.get("receipts"), payload.get("expenses")
    if isinstance(receipts, int) and isinstance(expenses, int):
        allowed.add(_norm_num(str(receipts - expenses)))
        percent = history.coverage_percent(receipts, expenses)
        if percent is not None:
            allowed.add(_norm_num(str(percent)))
    return allowed


def assert_text_faithful(text: str, payload: Mapping[str, Any]) -> None:
    """Raise when the generated text states a number the snapshot does not contain."""

    allowed = allowed_numbers(payload)
    allowed_decimals: set[Decimal] = set()
    for token in allowed:
        try:
            allowed_decimals.add(Decimal(token))
        except Exception:  # noqa: BLE001 - a non-numeric token is compared as text
            continue
    for token in _NUMBER_RE.findall(text):
        normalized = _norm_num(token)
        if normalized in allowed:
            continue
        try:
            number = Decimal(normalized)
        except Exception:  # noqa: BLE001
            raise ReportError(f"report text contains an unexplained token {token!r}")
        if number in allowed_decimals:
            continue
        raise ReportError(f"report text states a number the snapshot does not contain: {token!r}")
