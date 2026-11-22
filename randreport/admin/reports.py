"""Publication: the monthly draft post and the immutable closed versions.

Two data planes meet here and must never be mixed (docs/REQUIREMENTS.md §3.7,
§4 "Публикация", §10 "Суммирование не должно дважды добавлять период"):

* the **historical plane** — a published report of a past period, in whole
  rubles, tied to its public post;
* the **live plane** — the allowlist release the nightly builder publishes, in
  integer kopecks.

:func:`plane_for` names the plane a period is published from, and the draft post
is built from **one** of them: if both exist, the numbers are never added
together and the page says which plane it used. Closing a period freezes an
immutable version; every later correction arrives as a *new* version with a diff
and a reason, and the generated text is proved to state no number the version
does not contain.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .. import history
from ..domain.timeframes import Period, parse_period, range_period
from ..store import repository as repo
from ..store.db import transaction
from . import checks
from ..web import reports as reports_module

DRAFT = "draft"
READY = "ready"
CLOSED = "closed"

HISTORICAL = "historical"
LIVE = "live"
NONE = "none"

FIVE_SECTIONS = reports_module.FIVE_SECTIONS


class PublicationError(ValueError):
    """A publication action that must not be applied."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Plane:
    """Which data plane answers for a period, and with which figures."""

    kind: str
    label: str
    payload: Mapping[str, Any] | None
    note: str

    @property
    def available(self) -> bool:
        return self.kind != NONE


def _live_payload(conn: sqlite3.Connection, label: str) -> Mapping[str, Any] | None:
    """The published live release for a period, as whole-ruble report aggregates."""

    row = repo.current_release(conn)
    if row is None:
        return None
    payload = json.loads(row["payload"] or "{}")
    money = payload.get("money")
    events = payload.get("events")
    if not isinstance(money, Mapping):
        return None
    if str((money.get("period") or {}).get("requested") or "") != label:
        return None
    metrics = money.get("metrics") or {}
    events_metrics = (events or {}).get("metrics") or {}
    receipts = metrics.get("compensation_kopecks")
    expenses = metrics.get("expense_kopecks")
    return {
        "period": label,
        "events": events_metrics.get("total_events"),
        "receipts": receipts,
        "expenses": expenses,
        "unit": "RUB_kopecks",
        "plane": LIVE,
        "release_id": str(row["release_id"]),
        "source_label": "живая публичная проекция (ночной выпуск)",
        "coverage": str(money.get("completeness") or "unknown"),
        "note": "Живая проекция публикуется автоматически после ночной синхронизации; ручное закрытие её не блокирует.",
    }


def parse_report_label(label: str) -> Period:
    """``YYYY``, ``YYYY-MM`` or a whole-period range ``YYYY-MM..YYYY-MM``.

    A range label is a real report period: the historical archive stores a group
    report under ``2025-01..2025-12``, and the published snapshots seeded from it
    carry the same label, so the desk must be able to name the period instead of
    treating it as a broken one.
    """

    text = (label or "").strip()
    if ".." in text:
        start, end = text.split("..", 1)
        return range_period(start.strip(), end.strip())
    return parse_period(text)


def plane_for(conn: sqlite3.Connection, label: str) -> Plane:
    """Name the plane for a period without mixing the two planes' numbers."""

    try:
        period = parse_report_label(label)
    except Exception:  # noqa: BLE001 - an odd label is simply not publishable
        raise PublicationError("некорректный период отчёта")
    exact = history.exact_report(period)
    if exact is not None:
        return Plane(
            kind=HISTORICAL,
            label=label,
            payload=reports_module.snapshot_payload(exact),
            note="Историческая плоскость: опубликованный отчёт целого периода, суммы в целых рублях.",
        )
    stored = repo.latest_report_snapshot(conn, label)
    if stored is not None:
        payload = json.loads(stored["payload"])
        return Plane(
            kind=HISTORICAL,
            label=label,
            payload=payload,
            note=f"Историческая плоскость: зафиксированный снимок v{int(stored['version'])}.",
        )
    live = _live_payload(conn, label)
    if live is not None:
        return Plane(kind=LIVE, label=label, payload=live, note="Живая плоскость: выпуск ночного сборщика.")
    return Plane(kind=NONE, label=label, payload=None, note="Нет ни опубликованного отчёта, ни живого выпуска за этот период.")


def planes(conn: sqlite3.Connection, index: Any, label: str) -> dict[str, Any]:
    """Both planes as the desk sees them, with the overlap spelled out."""

    historical = [report.label for report in history.published_reports() if report.start == label or report.end == label or report.label == label]
    live = None if index is None else index.by_period(label)
    live_payload = _live_payload(conn, label)
    release_id = None
    if live is not None:
        release_id = live.release_id
    elif live_payload is not None:
        release_id = str(live_payload.get("release_id") or "выпуск из базы")
    overlap = bool(historical) and (live is not None or live_payload is not None)
    return {
        "label": label,
        "historical": historical,
        "live_release": release_id,
        "overlap": overlap,
        "note": (
            "За период есть и исторический агрегат, и живой выпуск: черновик берёт одну плоскость и не складывает их."
            if overlap
            else "Период публикуется из одной плоскости."
        ),
    }


# --------------------------------------------------------------------------- #
# draft post
# --------------------------------------------------------------------------- #

def _display(amount: Any, unit: str) -> str:
    """The *formatted* figure that goes into both the snapshot and the text.

    Whatever the text states must also exist in the payload, otherwise the
    faithfulness proof would have to be weakened; so the formatted strings are
    computed once and stored in the version, and the text only repeats them.
    """

    if amount is None:
        return "нет данных"
    if unit == "RUB_kopecks":
        rubles = (Decimal(int(amount)) / Decimal(100)).quantize(Decimal("0.01"))
        return f"{rubles:,.2f}".replace(",", "\u00a0").replace(".", ",") + " ₽"
    return f"{Decimal(int(amount)):,.0f}".replace(",", "\u00a0") + " ₽"


def _coverage_display(receipts: Any, expenses: Any) -> str:
    if not isinstance(receipts, int) or not isinstance(expenses, int) or expenses == 0:
        return "не применимо"
    percent = (Decimal(receipts) * Decimal(100) / Decimal(expenses)).quantize(Decimal("0.1"))
    return f"{str(percent).replace('.', ',')} %"


def _with_display(payload: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    unit = str(out.get("unit") or "RUB")
    receipts, expenses = out.get("receipts"), out.get("expenses")
    difference = receipts - expenses if isinstance(receipts, int) and isinstance(expenses, int) else None
    out["display"] = {
        "receipts": _display(receipts, unit),
        "expenses": _display(expenses, unit),
        "difference": _display(difference, unit),
        "coverage": _coverage_display(receipts, expenses),
    }
    return out


def draft_text(payload: Mapping[str, Any]) -> str:
    """The traditional five-section text, built only from the chosen plane."""

    payload = payload if "display" in payload else _with_display(payload)
    display = payload["display"]
    period = str(payload.get("period") or "")
    events = payload.get("events")
    lines = [f"Отчёт за {period}"]
    for heading in FIVE_SECTIONS:
        lines.append(f"\n{heading}")
        if heading == "Поступления":
            lines.append(f"Поступления: {display['receipts']}")
        elif heading == "Расходы":
            lines.append(f"Расходы: {display['expenses']}")
        elif heading == "Мероприятия":
            lines.append(f"Состоялось мероприятий: {events if events is not None else 'нет данных'}")
        elif heading == "Сообщество":
            lines.append("Подписчики: по этому снимку точек нет")
        else:
            if display["difference"] == "нет данных":
                lines.append("Итог: нет данных для разницы поступлений и расходов")
            else:
                lines.append(f"Разница (поступления − расходы): {display['difference']}, покрытие {display['coverage']}")
    lines.append(f"\nПлоскость данных: {payload.get('plane_label') or payload.get('plane')}")
    if payload.get("source_url"):
        lines.append(f"Источник: {payload['source_url']}")
    note = payload.get("note")
    if note:
        lines.append(str(note))
    lines.append(f"\n{history.NO_BALANCE_NOTE}")
    return "\n".join(lines)


def draft_payload(conn: sqlite3.Connection, label: str) -> dict[str, Any]:
    plane = plane_for(conn, label)
    if plane.payload is None:
        return _with_display(
            {
                "period": label,
                "plane": plane.kind,
                "plane_label": plane.note,
                "events": None,
                "receipts": None,
                "expenses": None,
                "unit": "RUB",
            }
        )
    payload = dict(plane.payload)
    payload["plane"] = plane.kind
    payload["plane_label"] = plane.note
    return _with_display(payload)


def build(conn: sqlite3.Connection, *, label: str, now: str | None = None) -> dict[str, Any]:
    """The preview: text + markdown + the readiness blockers, all from one plane."""

    payload = draft_payload(conn, label)
    text = draft_text(payload)
    reports_module.assert_text_faithful(text, payload)
    draft = _draft_row(conn, label)
    comment = str(draft["comment"]) if draft else ""
    markdown = to_markdown(payload, text=text, comment=comment)
    return {
        "period": label,
        "plane": payload.get("plane"),
        "payload": payload,
        "text": text,
        "markdown": markdown,
        "comment": comment,
        "status": str(draft["status"]) if draft else DRAFT,
        "frozen_version": None if not draft else draft["frozen_version"],
        "readiness": readiness(conn, label),
    }


def to_markdown(payload: Mapping[str, Any], *, text: str, comment: str = "") -> str:
    """Plain, copy-pasteable Markdown; no HTML is ever produced here."""

    body = "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines() if line.strip())
    header = f"## Отчёт за {payload.get('period')}"
    parts = [header, "", body]
    if comment.strip():
        parts += ["", f"Комментарий редакции: {comment.strip()}"]
    return "\n".join(parts).strip() + "\n"


def readiness(conn: sqlite3.Connection, label: str) -> dict[str, Any]:
    """What still blocks a confident publication of this period."""

    payload = draft_payload(conn, label)
    blockers: list[str] = []
    if payload.get("plane") == NONE:
        blockers.append("нет данных ни из одной плоскости")
    for field in ("receipts", "expenses"):
        if payload.get(field) is None:
            blockers.append(f"{field} неизвестны")
    state = checks.effective_state(conn, "period", label)
    if state != checks.VERIFIED:
        blockers.append(f"период не проверен (состояние: {state})")
    open_issues = [
        issue for issue in conn.execute("SELECT code, severity, entity_key FROM issues WHERE status != 'resolved'").fetchall()
    ]
    for issue in open_issues:
        if issue["entity_key"] in (None, label) or str(issue["entity_key"]).startswith(label):
            blockers.append(f"открытая проблема: {issue['code']}")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "check_state": state,
        "open_issues": len(open_issues),
        "plane": payload.get("plane"),
    }


def _draft_row(conn: sqlite3.Connection, label: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM post_drafts WHERE period_label = ?", (label,)).fetchone()


def save_draft(
    conn: sqlite3.Connection,
    *,
    label: str,
    actor: str,
    comment: str | None = None,
    status: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Store the manual comment and readiness marker — never the numbers."""

    if status is not None and status not in (DRAFT, READY, CLOSED):
        raise PublicationError("неизвестный статус черновика")
    current = _draft_row(conn, label)
    if status == READY:
        report = readiness(conn, label)
        if not report["ready"]:
            raise PublicationError("нельзя пометить готовым: " + "; ".join(report["blockers"]))
    stamp = now or _utcnow()
    text = None if comment is None else str(comment)
    if text is not None and len(text) > 1000:
        raise PublicationError("комментарий длиннее 1000 символов")
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO post_drafts (period_label, status, comment, markdown, frozen_version, updated_by, updated_at)
            VALUES (?, ?, ?, '', NULL, ?, ?)
            ON CONFLICT (period_label) DO UPDATE SET
                status = COALESCE(?, post_drafts.status),
                comment = COALESCE(?, post_drafts.comment),
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (
                label,
                status or (str(current["status"]) if current else DRAFT),
                "" if text is None else text,
                actor,
                stamp,
                status,
                text,
            ),
        )
    repo.audit(conn, action="post.draft.saved", actor=actor, entity_type="post_draft", entity_key=label, detail={"status": status}, now=stamp)
    return {"period": label, "status": status or (str(current["status"]) if current else DRAFT), "comment": text}


# --------------------------------------------------------------------------- #
# closing and correcting immutable versions
# --------------------------------------------------------------------------- #

def close_period(
    conn: sqlite3.Connection,
    *,
    label: str,
    actor: str,
    reason: str,
    create_version: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """Close a period: freeze the current picture as an immutable version.

    If no version exists yet, version 1 is written from the period's plane and is
    frozen; if versions exist, the latest one is frozen as it stands. A frozen
    version is never rewritten — corrections arrive as new versions.
    """

    if not str(reason or "").strip():
        raise PublicationError("закрытие периода требует причины")
    stamp = now or _utcnow()
    existing = repo.latest_report_snapshot(conn, label)
    version: int
    if existing is None and create_version:
        payload = draft_payload(conn, label)
        if payload.get("plane") == NONE:
            raise PublicationError("нечего фиксировать: за период нет данных ни из одной плоскости")
        provenance = reports_module.PUBLISHED if payload.get("plane") == HISTORICAL else reports_module.RECOMPUTED
        frozen = dict(payload)
        frozen["provenance"] = provenance
        frozen["coverage"] = str(payload.get("coverage") or history.COMPLETE)
        frozen["frozen_at"] = stamp
        frozen["frozen_by"] = actor
        version = repo.next_report_version(conn, label)
        repo.record_report_snapshot(
            conn,
            report_id=f"report:{label}:v{version}",
            period_label=label,
            version=version,
            published_at=stamp,
            payload=frozen,
            original_text=None,
            original_url=payload.get("source_url"),
        )
    elif existing is not None:
        version = int(existing["version"])
    else:  # pragma: no cover - defensive: closing without a version and without creation
        raise PublicationError("нет версии для закрытия")

    with transaction(conn):
        conn.execute(
            """
            INSERT INTO post_drafts (period_label, status, comment, markdown, frozen_version, updated_by, updated_at)
            VALUES (?, 'closed', '', '', ?, ?, ?)
            ON CONFLICT (period_label) DO UPDATE SET status = 'closed', frozen_version = excluded.frozen_version,
                updated_by = excluded.updated_by, updated_at = excluded.updated_at
            """,
            (label, version, actor, stamp),
        )
    repo.audit(
        conn,
        action="report.closed",
        actor=actor,
        entity_type="report_period",
        entity_key=label,
        detail={"version": version, "reason": reason},
        now=stamp,
    )
    return {"period": label, "version": version, "status": CLOSED}


def correction(
    conn: sqlite3.Connection,
    *,
    label: str,
    changes: Mapping[str, Any],
    reason: str,
    actor: str,
    now: str | None = None,
) -> dict[str, Any]:
    """A correction is a new version with a diff and a reason (R01)."""

    result = reports_module.record_correction(
        conn,
        period_label=label,
        changes=dict(changes),
        reason=reason,
        actor=actor,
        published_at=now,
    )
    stamp = now or _utcnow()
    repo.audit(
        conn,
        action="report.corrected",
        actor=actor,
        entity_type="report_period",
        entity_key=label,
        detail={"version": result["version"], "reason": reason, "fields": sorted(changes)},
        now=stamp,
    )
    checks.invalidate(conn, entity_type="report_period", entity_key=label, cause="correction", detail={"version": result["version"]}, now=stamp)
    checks.invalidate(conn, entity_type="period", entity_key=label, cause="correction", detail={"version": result["version"]}, now=stamp)
    return result


def versions(conn: sqlite3.Connection, label: str) -> tuple[dict[str, Any], ...]:
    return reports_module.versions(conn, label)


def text_for_version(conn: sqlite3.Connection, label: str, version: int) -> str | None:
    payload = reports_module.version_payload(conn, label, version)
    if payload is None:
        return None
    text = draft_text(payload)
    reports_module.assert_text_faithful(text, payload)
    return text


def export_markdown(conn: sqlite3.Connection, *, label: str, version: int | None = None) -> str:
    """Export the frozen text of a period (no HTML, no private fields)."""

    if version is not None:
        text = text_for_version(conn, label, version)
        if text is None:
            raise PublicationError("нет такой версии отчёта")
        payload = reports_module.version_payload(conn, label, version) or {}
    else:
        built = build(conn, label=label)
        return built["markdown"]
    return to_markdown(payload, text=text)


def publication_state(conn: sqlite3.Connection, label: str) -> dict[str, Any]:
    row = _draft_row(conn, label)
    latest = repo.latest_report_snapshot(conn, label)
    return {
        "period": label,
        "status": str(row["status"]) if row else DRAFT,
        "frozen_version": None if not row else row["frozen_version"],
        "latest_version": None if latest is None else int(latest["version"]),
        "versions": [{"version": int(item["version"]), "published_at": str(item["published_at"])} for item in versions(conn, label)],
        "readiness": readiness(conn, label),
    }


def exported_periods(conn: sqlite3.Connection, *, periods: Sequence[str] | None = None) -> dict[str, str]:
    """The single export bundle the desk hands over: one Markdown per period."""

    labels = list(periods) if periods else list(repo.report_period_labels(conn))
    return {label: export_markdown(conn, label=label) for label in labels}
