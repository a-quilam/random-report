"""Diagnostics for the editorial desk (docs/REQUIREMENTS.md section 4, "Сверка").

The desk must answer, for every discrepancy: *what* it is, *which sources* it
came from, *which periods* it touches, how significant it is, what its status is
and what a human wrote about it. Nothing here repairs anything:

* a finding is recomputed from the stored facts on every request; the database
  keeps its current state and its comment;
* a manual "verified" does **not** close a finding — a finding closes only when
  the condition that produced it stops being detectable;
* a private figure never reaches a finding: findings carry aggregates, category
  ids and periods, not raw bank rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from ..domain.finance import build_ledger, compute_totals, consolidated_balance
from ..domain.timeframes import range_period
from ..ops import status as ops_status
from ..store import repository as repo
from ..store.db import transaction
from . import history_admin

OPEN = "open"
ACKNOWLEDGED = "acknowledged"
RESOLVED = "resolved"

SEVERITIES = ("info", "warning", "high")

# Codes this module owns. An issue of one of these codes that is no longer
# detected is marked resolved; any other issue is left untouched.
CATALOG = (
    "category_unmapped",
    "currency_unknown",
    "source_changed",
    "duplicate_rows",
    "coverage_gap",
    "source_unreliable",
    "balance_not_reconciling",
    "historical_conflict",
    "historical_unresolved",
    "historical_gap",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    explanation: str
    significance: str
    sources: tuple[str, ...] = ()
    periods: tuple[str, ...] = ()
    entity_type: str | None = None
    entity_key: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def issue_id(self) -> str:
        raw = "|".join([self.code, self.entity_type or "", self.entity_key or ""])
        return "iss_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _policy(conn: sqlite3.Connection):
    from ..web.build import policy_from_store

    return policy_from_store(conn)


def _active_transactions(conn: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    return tuple(conn.execute("SELECT * FROM finance_transactions WHERE deleted = 0").fetchall())


def _manifest(conn: sqlite3.Connection) -> Mapping[str, Any]:
    loaded = repo.load_category_mapping(conn)
    return {} if loaded is None else dict(loaded[1])


def _category_unmapped(conn: sqlite3.Connection) -> tuple[Finding, ...]:
    manifest = _manifest(conn)
    known = set(str(key) for key in (manifest.get("category_classes") or {}))
    rows = _active_transactions(conn)
    unknown: dict[str, dict[str, Any]] = {}
    for row in rows:
        category_id = row["category_id"]
        if category_id is None or str(category_id) in known:
            continue
        key = str(category_id)
        entry = unknown.setdefault(key, {"count": 0, "sum_kopecks": 0})
        entry["count"] += 1
        entry["sum_kopecks"] += abs(int(row["amount_kopecks"]))
    out: list[Finding] = []
    for category_id, entry in sorted(unknown.items()):
        out.append(
            Finding(
                code="category_unmapped",
                severity="high",
                title="Категория вне подтверждённого публичного контура",
                explanation=(
                    "Операции этой категории не отнесены к публичному контуру: они не попадают в публичные "
                    "суммы, пока не задано сопоставление категории. Суммы — только по заведомо публичным операциям."
                ),
                significance="Пока сопоставление не задано, денежные агрегаты неполны.",
                sources=("zenmoney",),
                entity_type="finance_category",
                entity_key=category_id,
                payload={"operations": entry["count"], "sum_kopecks": entry["sum_kopecks"]},
            )
        )
    return tuple(out)


def _currency_unknown(conn: sqlite3.Connection) -> tuple[Finding, ...]:
    rows = [row for row in _active_transactions(conn) if str(row["currency"] or "RUB") != "RUB"]
    if not rows:
        return ()
    groups: dict[str, int] = {}
    for row in rows:
        groups[str(row["currency"])] = groups.get(str(row["currency"]), 0) + 1
    return tuple(
        Finding(
            code="currency_unknown",
            severity="high",
            title="Операции в иной валюте без подтверждённого курса",
            explanation=(
                "Суммы в валюте не пересчитываются задним числом по текущему курсу: без фактической рублёвой оценки "
                "операции помечаются как неизвестные, а полнота публичного раздела становится неполной."
            ),
            significance="Пересчёт по выдуманному курсу исказил бы все агрегаты за период.",
            sources=("zenmoney",),
            entity_type="finance_category",
            entity_key=f"currency:{currency}",
            payload={"currency": currency, "operations": count},
        )
        for currency, count in sorted(groups.items())
    )


def _source_changed(conn: sqlite3.Connection) -> tuple[Finding, ...]:
    out: list[Finding] = []
    deleted_moves = conn.execute(
        "SELECT transaction_id, occurred_on FROM finance_transactions WHERE deleted = 1 ORDER BY occurred_on"
    ).fetchall()
    if deleted_moves:
        out.append(
            Finding(
                code="source_changed",
                severity="warning",
                title="Операции удалены или изменены источником",
                explanation="Источник вернул удаление или новую ревизию: строка не участвует в сумме, но её внутренняя ревизия сохранена.",
                significance="Публичные суммы пересчитываются; отметки проверки этих строк сбрасываются.",
                sources=("zenmoney",),
                periods=tuple(sorted({str(row["occurred_on"])[:7] for row in deleted_moves})),
                entity_type="finance_transaction",
                entity_key=None,
                payload={"operations": len(deleted_moves)},
            )
        )
    changed = conn.execute(
        "SELECT calendar_id, occurrence_id FROM calendar_occurrences WHERE deleted = 1 OR source_present = 0 ORDER BY calendar_id"
    ).fetchall()
    if changed:
        out.append(
            Finding(
                code="source_changed",
                severity="warning",
                title="События удалены источником или исчезли по сроку хранения",
                explanation=(
                    "Явное удаление убирает событие из оперативной статистики с сохранением внутренней ревизии; "
                    "исчезновение по retention помечается локальным архивом и не стирает историю."
                ),
                significance="Мероприятия затронутых периодов пересчитываются; архив остаётся.",
                sources=("teamup",),
                entity_type="calendar_occurrence",
                entity_key=None,
                payload={"occurrences": len(changed)},
            )
        )
    return tuple(out)


def _duplicates(conn: sqlite3.Connection) -> tuple[Finding, ...]:
    rows = conn.execute(
        """
        SELECT occurred_on, amount_kopecks, source_account, target_account, category_id, COUNT(*) AS n
          FROM finance_transactions
         WHERE deleted = 0 AND pair_id IS NULL
         GROUP BY occurred_on, amount_kopecks, source_account, target_account, category_id
        HAVING n > 1
        """
    ).fetchall()
    return tuple(
        Finding(
            code="duplicate_rows",
            severity="warning",
            title="Похожие операции без пары переводов",
            explanation=(
                "Совпадают дата, сумма, счета и категория, но связи пары нет: возможен дубль или две действительно "
                "одинаковые операции. Дубли не сливаются автоматически."
            ),
            significance="Пока не проверено, сумма за период может быть завышена.",
            sources=("zenmoney",),
            periods=(str(row["occurred_on"])[:7],),
            entity_type="period",
            entity_key=str(row["occurred_on"])[:7],
            payload={"occurrences": int(row["n"]), "amount_kopecks": int(row["amount_kopecks"])},
        )
        for row in rows
    )


def _coverage_gaps(conn: sqlite3.Connection) -> tuple[Finding, ...]:
    rows = conn.execute("SELECT * FROM source_coverage WHERE coverage != 'complete'").fetchall()
    return tuple(
        Finding(
            code="coverage_gap",
            severity="warning",
            title="Неполное покрытие периода источником",
            explanation="Источник не подтверждает полное покрытие этого периода: данные показываются с пометкой неполноты.",
            significance="Неполный период не сравнивается с полным молча и не выдаёт точный процент покрытия.",
            sources=(str(row["source"]),),
            periods=(f"{row['period_from']}..{row['period_to']}",),
            entity_type="period",
            entity_key=str(row["period_from"]),
            payload={"coverage": str(row["coverage"]), "grain": str(row["period_grain"])},
        )
        for row in rows
    )


def _source_reliability(conn: sqlite3.Connection, *, now: datetime | None = None) -> tuple[Finding, ...]:
    out: list[Finding] = []
    for row in repo.source_runs(conn):
        if str(row["status"]) in ("failed", "partial") or row["error"]:
            out.append(
                Finding(
                    code="source_unreliable",
                    severity="high" if str(row["status"]) == "failed" else "warning",
                    title=f"Источник {row['source']} завершил запуск со статусом {row['status']}",
                    explanation="Успешные данные не обновлялись: раздел продолжает отдавать последнюю проверенную версию с пометкой «устарело», ноль не подставляется.",
                    significance=f"Число ошибок источника: {row['error_count']}. Ошибка не равна нулю и не стирает данные.",
                    sources=(str(row["source"]),),
                    periods=(str(row["logical_date"]),),
                    entity_type="source_run",
                    entity_key=str(row["run_id"]),
                    payload={"status": str(row["status"]), "error_count": int(row["error_count"])},
                )
            )
    # freshness of each source that has ever reported
    sources = sorted({str(row["source"]) for row in repo.source_runs(conn)})
    for source in sources:
        state = ops_status.source_status(conn, source, now=now)
        if state.freshness != "fresh" and not state.last_success_at:
            out.append(
                Finding(
                    code="source_unreliable",
                    severity="warning",
                    title=f"У источника {source} нет успешного запуска",
                    explanation="Ни одного успешно проверенного запуска: раздел этого источника пуст, а не нулевой.",
                    significance="Ноль и «нет данных» различимы в публичном интерфейсе.",
                    sources=(source,),
                    entity_type="source_run",
                    entity_key=f"{source}:no-success",
                    payload={"freshness": state.freshness},
                )
            )
    return tuple(out)


def _balance_reconciliation(conn: sqlite3.Connection, *, now: datetime | None = None) -> tuple[Finding, ...]:
    """cash_delta vs the operating result — reported, never "fixed"."""

    balances = repo.load_balances(conn)
    dated = sorted(balance.as_of for balance in balances if balance.as_of is not None)
    if len(dated) < 2 or dated[0] == dated[-1]:
        return ()
    period = range_period(f"{dated[0].year:04d}-{dated[0].month:02d}", f"{dated[-1].year:04d}-{dated[-1].month:02d}")
    policy = _policy(conn)
    movements = repo.load_movements(conn)
    ledger = build_ledger(movements, policy, period, source_data_present=True)
    totals = compute_totals(ledger, as_of=period.end)
    summary = consolidated_balance(balances, as_of=None, scope_policy=policy)
    if summary.kopecks is None:
        return ()
    opening = [balance for balance in balances if balance.as_of == dated[0]]
    closing = [balance for balance in balances if balance.as_of == dated[-1]]
    delta = sum(int(balance.balance_kopecks) for balance in closing) - sum(int(balance.balance_kopecks) for balance in opening)
    operating = totals.result_before_subsidy_kopecks
    if operating is None:
        return ()
    difference = delta - operating
    if difference == 0:
        return ()
    return (
        Finding(
            code="balance_not_reconciling",
            severity="warning",
            title="Изменение остатка не совпадает с результатом деятельности",
            explanation=(
                "Изменение остатка и результат деятельности считаются по-разному: переводы, прямые оплаты коммунаров, "
                "займы и время учёта операций объясняют расхождение. Остаток не подменяется дефицитом."
            ),
            significance=f"Расхождение {difference} копеек за {period.label}; остаток публикуется только суммарно.",
            sources=("zenmoney",),
            periods=(period.label,),
            entity_type="period",
            entity_key=period.label,
            payload={"cash_delta_kopecks": delta, "operating_result_kopecks": int(operating), "difference_kopecks": difference},
        ),
    )


def _historical_findings(conn: sqlite3.Connection) -> tuple[Finding, ...]:
    out: list[Finding] = []
    for conflict in history_admin.conflicts(conn, status="open"):
        out.append(
            Finding(
                code="historical_conflict",
                severity="high",
                title="Историческое расхождение открыто",
                explanation=conflict.notes or "Историческое расхождение опубликованных данных.",
                significance="Оригинальная публикация остаётся зафиксированной; пересчитанное значение не подменяет её без версии.",
                sources=("history_archive",),
                periods=tuple(p for p in (conflict.period_from, conflict.period_to) if p),
                entity_type="historical_conflict",
                entity_key=conflict.conflict_id,
                payload={
                    "group": conflict.conflict_group,
                    "published_value": conflict.published_value,
                    "computed_value": conflict.computed_value,
                    "difference": conflict.difference,
                },
            )
        )
    unresolved = history_admin.unresolved(conn)
    if unresolved:
        out.append(
            Finding(
                code="historical_unresolved",
                severity="warning",
                title="Наблюдения, которые нельзя считать фактами",
                explanation=(
                    "Неоднозначный год, пересказ, черновой вариант книги и неполный PDF-календарь хранятся как наблюдения. "
                    "Они не входят в публичные агрегаты и не могут быть помечены проверенными."
                ),
                significance="Историческое покрытие показывается как частичное там, где источника нет.",
                sources=("history_archive",),
                entity_type="historical_observation",
                entity_key="unresolved",
                payload={"count": len(unresolved), "statuses": sorted({item.status for item in unresolved})},
            )
        )
    for gap in history_admin.gaps(conn):
        out.append(
            Finding(
                code="historical_gap",
                severity="warning",
                title=f"Публичные месяцы {gap['year']} года покрыты не полностью",
                explanation=gap["note"],
                significance="Годовой агрегат не делится пропорционально: месяцы без публикации остаются неизвестными.",
                sources=("history_archive",),
                periods=tuple(gap["missing"]),
                entity_type="historical_gap",
                entity_key=f"{gap['year']}:{gap['metric']}",
                payload={"missing": gap["missing"], "covered": gap["covered"]},
            )
        )
    return tuple(out)


DETECTORS: tuple[Callable[..., tuple[Finding, ...]], ...] = (
    _category_unmapped,
    _currency_unknown,
    _source_changed,
    _duplicates,
    _coverage_gaps,
    _source_reliability,
    _balance_reconciliation,
    _historical_findings,
)


def collect(conn: sqlite3.Connection, *, now: datetime | None = None) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for detector in DETECTORS:
        findings.extend(detector(conn, now=now) if detector in (_source_reliability, _balance_reconciliation) else detector(conn))
    return tuple(findings)


# --------------------------------------------------------------------------- #
# issues: status, comment, and resolution only when the condition disappears
# --------------------------------------------------------------------------- #

def sync_issues(conn: sqlite3.Connection, findings: Sequence[Finding], *, now: str | None = None) -> dict[str, int]:
    """Upsert the current findings; resolve only ones that are no longer detected."""

    stamp = now or _utcnow()
    current = {finding.issue_id: finding for finding in findings}
    inserted = updated = resolved = 0
    existing = {str(row["issue_id"]): dict(row) for row in conn.execute("SELECT * FROM issues").fetchall()}
    for issue_id, finding in current.items():
        row = existing.get(issue_id)
        with transaction(conn):
            if row is None:
                conn.execute(
                    "INSERT INTO issues (issue_id, code, severity, entity_type, entity_key, payload, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                    (
                        issue_id,
                        finding.code,
                        finding.severity,
                        finding.entity_type,
                        finding.entity_key,
                        json.dumps(dict(finding.payload), ensure_ascii=False),
                        stamp,
                    ),
                )
                inserted += 1
            else:
                conn.execute(
                    "UPDATE issues SET severity = ?, payload = ? WHERE issue_id = ?",
                    (finding.severity, json.dumps(dict(finding.payload), ensure_ascii=False), issue_id),
                )
                updated += 1
    for issue_id, row in existing.items():
        if str(row["code"]) not in CATALOG or issue_id in current or str(row["status"]) == RESOLVED:
            continue
        with transaction(conn):
            conn.execute("UPDATE issues SET status = ? WHERE issue_id = ?", (RESOLVED, issue_id))
        repo.audit(conn, action="issue.resolved", actor="system", entity_type="issue", entity_key=issue_id, detail={"reason": "условие больше не обнаруживается"}, now=stamp)
        resolved += 1
    return {"inserted": inserted, "updated": updated, "resolved": resolved, "open": len(current)}


def acknowledge(conn: sqlite3.Connection, issue_id: str, *, actor: str, comment: str, now: str | None = None) -> bool:
    """A human note on a discrepancy — it stays open."""

    stamp = now or _utcnow()
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE issues SET status = ?, payload = json_set(payload, '$.comment', ?, '$.acknowledged_by', ?) WHERE issue_id = ? AND status != ?",
            (ACKNOWLEDGED, comment, actor, issue_id, RESOLVED),
        )
    if cursor.rowcount:
        repo.audit(conn, action="issue.acknowledged", actor=actor, entity_type="issue", entity_key=issue_id, detail={"comment": comment}, now=stamp)
    return bool(cursor.rowcount)


def issues(conn: sqlite3.Connection, *, status: str | None = None) -> tuple[dict[str, Any], ...]:
    sql = "SELECT * FROM issues"
    args: tuple[Any, ...] = ()
    if status is not None:
        sql += " WHERE status = ?"
        args = (status,)
    rows = conn.execute(sql + " ORDER BY severity DESC, created_at", args).fetchall()
    return tuple(dict(row) for row in rows)


def issue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM issues GROUP BY status"):
        counts[str(row["status"])] = int(row["n"])
    return counts


def comment_of(issue: Mapping[str, Any]) -> str:
    try:
        payload = json.loads(str(issue.get("payload") or "{}"))
    except ValueError:  # pragma: no cover - defensive
        return ""
    return str(payload.get("comment") or "")


def decimal_kopecks(value: int | None) -> Decimal | None:
    """Money in kopecks stays integral; Decimal is only for formatted output."""

    return None if value is None else Decimal(value)
