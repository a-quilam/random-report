"""Server-side rendering of the eight public sections.

Every string in this module is either a fixed label or a value taken from a
strict DTO / a historical observation. Nothing here reads a source, computes
money, or reaches for an internal id, so the rendered HTML inherits the
allowlist of the projection layer (docs/DOMAIN.md sections 8-10).

Accessibility contract (section 3.9, acceptance U01): one ``main`` landmark, a
skip link, keyboard-operable navigation and period switcher, focus outlines that
are never removed, WCAG-AA contrast in the stylesheet, statuses written as text
rather than colour alone, charts as inline SVG *plus* an equivalent data table,
and a layout that survives a 360 px viewport with horizontally scrollable tables.
"""

from __future__ import annotations

import calendar
from decimal import Decimal
from html import escape as _escape
from typing import Any, Iterable, Mapping, Sequence

from .. import history
from ..domain.dto import (
    CategoryDTO,
    ChronicleDTO,
    CommunityDTO,
    EventsDTO,
    MoneyDTO,
    PublicEventDTO,
)
from ..domain.money import format_kopecks, share_percent
from ..domain.timeframes import month_bounds

SITE_NAME = "А дорог ли РанДом?"
SITE_TAGLINE = "Сколько стоит наш подвал и что мы на эти деньги делаем"

NAV: tuple[tuple[str, str], ...] = (
    ("/", "Главная"),
    ("/money", "Деньги"),
    ("/events", "Мероприятия"),
    ("/community", "Сообщество"),
    ("/support", "Поддержка"),
    ("/chronicle", "Летопись"),
    ("/reports", "Архив"),
    ("/methodology", "Методология"),
)

EVENT_TYPE_LABELS = {
    "open": "открытые",
    "open_by_registration": "открытые по записи",
    "closed": "закрытые",
    "internal_meeting": "внутренние собрания",
    "preparation": "репетиции и подготовка",
    "unknown": "тип не указан",
}

FRESHNESS_LABELS = {
    "fresh": "свежие данные",
    "stale": "данные устарели",
    "missing": "нет данных",
    "unknown": "состояние неизвестно",
    "dev": "данные стенда",
}

VERIFICATION_LABELS = {
    "operational": "оперативные, без ручной проверки",
    "verified": "проверено",
    "conflict": "есть расхождение",
    "review": "ждёт проверки",
    "unspecified": "состояние проверки не указано",
}

COMPLETENESS_LABELS = {
    "complete": "полный период",
    "partial": "период неполон",
    "unknown": "полнота неизвестна",
}


def e(value: Any) -> str:
    return _escape("" if value is None else str(value), quote=True)


_SAFE_URL_SCHEMES = frozenset({"http", "https"})


def safe_url(value: Any) -> str:
    """Return ``value`` only when it is an http(s) URL or a same-origin path.

    A page never renders a link whose URL came from a source document: a
    ``javascript:`` or ``data:`` scheme (or a protocol-relative ``//host``) would
    turn a stored document into script execution or an open redirect. Anything
    else collapses to an empty string, so the anchor renders without a target
    rather than with an unsafe one.
    """

    text = ("" if value is None else str(value)).strip()
    if not text:
        return ""
    if text.startswith("/") and not text.startswith("//"):
        return text
    from urllib.parse import urlsplit

    try:
        scheme = urlsplit(text).scheme.lower()
    except ValueError:  # pragma: no cover - defensive
        return ""
    return text if scheme in _SAFE_URL_SCHEMES else ""


def rub_from_kopecks(kopecks: int | None) -> str:
    if kopecks is None:
        return "нет данных"
    return f"{format_kopecks(kopecks)} ₽"


def rub_from_rubles(rubles: int | None) -> str:
    if rubles is None:
        return "нет данных"
    return f"{rubles:,.0f}".replace(",", " ") + " ₽"


def percent_text(value: str | None) -> str:
    return "не применимо" if value is None else f"{value} %"


def _delta_text(value: int | None, unit: str) -> str:
    if value is None:
        return "нет данных"
    sign = "+" if value > 0 else ""
    return f"{sign}{value} {unit}"


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

def period_options(periods: Sequence[str], selected: str) -> str:
    """A grouped, keyboard-operable period list; the current value stays selected."""

    months = [p for p in periods if len(p) == 7]
    years = [p for p in periods if len(p) == 4]
    known = set(periods) | {selected}
    groups: list[str] = []
    if selected not in periods:
        groups.append(f'<option value="{e(selected)}" selected>{e(selected)}</option>')
    for label, values in (("Месяцы", months), ("Годы", years)):
        if not values:
            continue
        body = "".join(
            f'<option value="{e(v)}"{" selected" if v == selected else ""}>{e(v)}</option>' for v in values
        )
        groups.append(f'<optgroup label="{e(label)}">{body}</optgroup>')
    if "all" in known:
        groups.append(f'<option value="all"{" selected" if selected == "all" else ""}>Всё время</option>')
    return "".join(groups)


def period_switcher(action: str, periods: Sequence[str], selected: str, label: str = "Период") -> str:
    options = period_options(periods, selected)
    return (
        f'<form class="period" method="get" action="{e(action)}" role="search" aria-label="Выбор периода">'
        f'<label for="period-select">{e(label)}</label>'
        f'<select id="period-select" name="period">{options}</select>'
        f'<button type="submit">Показать</button>'
        f"</form>"
    )


def nav(active: str, period: str) -> str:
    items = []
    for href, label in NAV:
        current = ' aria-current="page"' if href == active else ""
        query = f"?period={e(period)}" if period and period != "all" else ""
        items.append(f'<li><a href="{e(href)}{query}"{current}>{e(label)}</a></li>')
    return f'<nav class="sections" aria-label="Разделы сайта"><ul>{"".join(items)}</ul></nav>'


def status_line(
    *,
    period_label: str,
    as_of: str | None,
    freshness: str,
    verification: str,
    completeness: str,
    source_labels: Sequence[str] = (),
    release_id: str | None = None,
) -> str:
    parts = [
        f'<span class="status"><span class="status-key">Период</span> <span class="status-value">{e(period_label)}</span></span>',
        f'<span class="status"><span class="status-key">Данные на</span> <span class="status-value">{e(as_of or "неизвестно")}</span></span>',
        f'<span class="status"><span class="status-key">Свежесть</span> <span class="status-value">{e(FRESHNESS_LABELS.get(freshness, freshness))}</span></span>',
        f'<span class="status"><span class="status-key">Проверка</span> <span class="status-value">{e(VERIFICATION_LABELS.get(verification, verification))}</span></span>',
        f'<span class="status"><span class="status-key">Полнота</span> <span class="status-value">{e(COMPLETENESS_LABELS.get(completeness, completeness))}</span></span>',
    ]
    if source_labels:
        parts.append(
            f'<span class="status"><span class="status-key">Источники</span> <span class="status-value">{e(", ".join(source_labels))}</span></span>'
        )
    if release_id:
        parts.append(f'<span class="status"><span class="status-key">Выпуск</span> <span class="status-value">{e(release_id)}</span></span>')
    return f'<p class="statusbar">{"".join(parts)}</p>'


def layout(
    *,
    title: str,
    active: str,
    period: str,
    periods: Sequence[str],
    body: str,
    status: str = "",
    description: str = SITE_TAGLINE,
    scripts: str = "",
) -> str:
    csv_safe = len(period) in (4, 7)
    csv_link = (
        f'<a href="/api/public/export/categories.csv?period={e(period)}">CSV агрегатов за период</a>'
        if csv_safe
        else '<a href="/reports">Архив отчётов</a>'
    )
    return (
        "<!doctype html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)} · {e(SITE_NAME)}</title>"
        f'<meta name="description" content="{e(description)}">'
        '<link rel="stylesheet" href="/static/site.css">'
        '<link rel="alternate" type="application/json" href="/api/public/overview">'
        "</head><body>"
        '<a class="skip" href="#content">Перейти к содержанию</a>'
        '<header class="site-header">'
        f'<p class="brand"><a href="/?period={e(period)}">{e(SITE_NAME)}</a></p>'
        f'<p class="tagline">{e(SITE_TAGLINE)}</p>'
        f"{nav(active, period)}"
        f"{period_switcher(active, periods, period)}"
        "</header>"
        f"{status}"
        f'<main id="content" tabindex="-1">{body}</main>'
        '<footer class="site-footer">'
        '<p>Публичный сайт без регистрации. Денежные суммы — фактические номинальные рубли, без инфляционной поправки.</p>'
        f'<p><a href="/methodology?period={e(period)}">Как считаются цифры</a> · '
        f"{csv_link} · "
        f'<a href="{e(history.CHANNEL_URL)}" rel="noopener">Публичный канал</a></p>'
        "</footer>"
        f"{scripts}"
        "</body></html>"
    )


def empty_state(message: str, *, hint: str = "") -> str:
    hint_html = f'<p class="hint">{e(hint)}</p>' if hint else ""
    return f'<div class="empty" role="status"><p>{e(message)}</p>{hint_html}</div>'


# --------------------------------------------------------------------------- #
# Generic components
# --------------------------------------------------------------------------- #

def data_table(
    caption: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    numeric_from: int = 1,
    classes: str = "",
    foot: Sequence[Any] | None = None,
) -> str:
    head = "".join(f'<th scope="col">{e(h)}</th>' for h in headers)
    body = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            tag = "th" if index == 0 else "td"
            scope = ' scope="row"' if index == 0 else ""
            align = ' class="num"' if index >= numeric_from and index > 0 else ""
            cells.append(f"<{tag}{scope}{align}>{cell}</{tag}>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    foot_html = ""
    if foot is not None:
        cells = []
        for index, cell in enumerate(foot):
            tag = "th" if index == 0 else "td"
            scope = ' scope="row"' if index == 0 else ""
            cells.append(f"<{tag}{scope}>{cell}</{tag}>")
        foot_html = f"<tfoot><tr>{''.join(cells)}</tr></tfoot>"
    return (
        f'<div class="table-wrap"><table class="{e(classes)}">'
        f"<caption>{e(caption)}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody>{foot_html}</table></div>"
    )


def coverage_bar(
    segments: Sequence[tuple[str, Decimal | None]],
    *,
    total_percent: Decimal | None = None,
    missing_label: str = "недостающая часть",
) -> str:
    """A 100 % coverage bar built from labelled data segments.

    Segments never become negative and the real percentage is never clipped: when
    the coverage exceeds 100 % the segments simply fill the bar and no "missing"
    part is drawn. The bar is one ``role="img"`` element with a text label, and an
    equivalent table follows it for an assistive-technology reading of the data.
    """

    described: list[str] = []
    for label, value in segments:
        described.append(f"{label}: {'нет данных' if value is None else f'{value} %'}")
    headline = "не применимо" if total_percent is None else f"{total_percent} %"
    missing = None if total_percent is None else max(Decimal(0), Decimal(100) - total_percent)

    drawn: list[list[Any]] = []
    for label, value in segments:
        if value is None or value <= 0:
            continue
        drawn.append([label, value, min(value, Decimal(100))])
    used = sum((item[2] for item in drawn), Decimal(0))
    # The drawn widths always fill exactly the room the authoritative coverage
    # leaves, so the bar reads as a truthful 100 %; the printed percentages stay
    # the exact rounded shares (they are repeated verbatim in the table below).
    available = Decimal(100) if missing is None else Decimal(100) - missing
    if used > 0 and available > 0:
        factor = available / used
        for item in drawn:
            item[2] = (item[2] * factor).quantize(Decimal("0.01"))
        drift = available - sum((item[2] for item in drawn), Decimal(0))
        drawn[-1][2] = drawn[-1][2] + drift
        used = available
    parts = [
        f'<span class="seg seg-{index % 4}" style="width:{item[2]}%">'
        f'<span class="seg-label">{e(item[0])} {item[1]} %</span></span>'
        for index, item in enumerate(drawn)
    ]
    if missing is not None and missing > 0:
        parts.append(
            f'<span class="seg seg-missing" style="width:{missing}%">'
            f'<span class="seg-label">{e(missing_label)} {missing} %</span></span>'
        )
    label_text = f"Покрытие расходов поступлениями: {headline}. " + "; ".join(described)
    table_rows = [(label, "нет данных" if value is None else f"{value} %") for label, value in segments]
    table_rows.append(("Недостающая часть", "нет данных" if missing is None else f"{missing} %"))
    table_rows.append(("Покрытие", headline))
    return (
        f'<figure class="coverage">'
        f'<figcaption>Покрытие расходов: <strong>{e(headline)}</strong></figcaption>'
        f'<div class="bar" role="img" aria-label="{e(label_text)}">{"".join(parts)}</div>'
        f"</figure>"
        + data_table(
            "Покрытие расходов по источникам (текстовая версия графика)",
            ("Источник", "Доля"),
            [(e(label), e(value)) for label, value in table_rows],
            numeric_from=1,
            classes="chart-alt",
        )
    )


def line_chart(points: Sequence[tuple[str, Decimal | None]], *, title: str, unit: str) -> str:
    """A monthly/yearly trend as SVG plus its data table (section 3.9)."""

    known = [(label, value) for label, value in points if value is not None]
    if not known:
        return data_table(
            f"{title} (нет данных)",
            ("Период", unit),
            [(e(label), "нет данных") for label, _ in points],
        )
    values = [float(value) for _, value in known]
    peak = max(values) or 1.0
    step = 100 / max(len(known) - 1, 1)
    coords = " ".join(f"{index * step:.2f},{100 - (value / peak * 90)}" for index, value in enumerate(values))
    desc = "; ".join(f"{label}: {value} {unit}" for label, value in known)
    svg = (
        f'<svg class="trend" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" '
        f'aria-label="{e(title)}. {e(desc)}">'
        f"<title>{e(title)}</title>"
        f"<desc>{e(desc)}</desc>"
        f'<polyline points="{coords}" class="trend-line"></polyline>'
        "</svg>"
    )
    table = data_table(
        f"{title} (текстовая версия графика)",
        ("Период", unit),
        [(e(label), "нет данных" if value is None else e(value)) for label, value in points],
        numeric_from=1,
        classes="chart-alt",
    )
    return f'<figure class="trend-figure">{svg}{table}</figure>'


def note(text: str, *, kind: str = "info") -> str:
    return f'<p class="note note-{e(kind)}">{e(text)}</p>'


def provenance(report: history.HistoricalReport, *, extra: str = "") -> str:
    tail = f" {e(extra)}" if extra else ""
    return (
        f'<p class="provenance">Источник: <a href="{e(safe_url(report.url))}" rel="noopener">'
        f"публикация {e(report.published)}</a>.{tail}</p>"
    )


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

def coverage_segments(money: MoneyDTO) -> list[tuple[str, Decimal | None]]:
    """Split the compensation of a period into its real sources.

    The segments come from the category rows themselves (largest first, the tail
    named "прочее" only when there is a tail) — no category is hardcoded and no
    name is invented; the missing part is left to the bar.
    """

    expense = money.metrics.get("expense_kopecks")
    if not expense:
        return []
    sources = [c for c in money.categories if c.bucket == "external_compensation"]
    if not sources:
        total = money.metrics.get("compensation_kopecks")
        return [("компенсация", share_percent(total or 0, expense))]
    ordered = sorted(sources, key=lambda c: -c.amount_kopecks)
    top, tail = ordered[:3], ordered[3:]
    segments: list[tuple[str, Decimal | None]] = [(c.label, share_percent(c.amount_kopecks, expense)) for c in top]
    if tail:
        segments.append(("прочее", share_percent(sum(c.amount_kopecks for c in tail), expense)))
    return segments


def money_section(money: MoneyDTO | None, *, period_label: str, historical: Mapping[str, "history.MetricView | None"]) -> str:
    blocks: list[str] = ["<h1>Деньги</h1>"]
    if money is not None:
        metrics = money.metrics
        coverage_value = Decimal(metrics["coverage_percent"]) if metrics.get("coverage_percent") else None
        blocks.append(coverage_bar(coverage_segments(money), total_percent=coverage_value))
    if money is None:
        blocks.append(
            empty_state(
                "Текущих финансовых данных за этот период нет.",
                hint="Финансовая проекция появляется после успешного ночного сбора. Ниже — только опубликованные исторические агрегаты.",
            )
        )
    else:
        metrics = money.metrics
        blocks.append('<h2>Текущий период</h2>')
        blocks.append(
            data_table(
                "Финансовые показатели периода",
                ("Показатель", "Значение"),
                [
                    ("Расходы", rub_from_kopecks(metrics.get("expense_kopecks"))),
                    ("Возвраты расходов", rub_from_kopecks(metrics.get("expense_refund_kopecks"))),
                    ("Компенсация (поступления от деятельности и донаты)", rub_from_kopecks(metrics.get("compensation_kopecks"))),
                    ("Фактический вклад коммунаров", rub_from_kopecks(metrics.get("subsidy_kopecks"))),
                    ("Разница между затратами и компенсацией", rub_from_kopecks(metrics.get("uncovered_kopecks"))),
                    ("Покрытие расходов", percent_text(metrics.get("coverage_percent"))),
                    ("Покрытие окончательно", "да" if metrics.get("coverage_is_final") else "нет (есть неполнота)"),
                    ("Непокрытая часть", rub_from_kopecks(metrics.get("shortfall_kopecks"))),
                    ("Превышение поступлений", rub_from_kopecks(metrics.get("excess_kopecks"))),
                    ("Результат до субсидии", rub_from_kopecks(metrics.get("result_before_subsidy_kopecks"))),
                    ("Результат после субсидии", rub_from_kopecks(metrics.get("result_after_subsidy_kopecks"))),
                    ("Остаток средств на дату снимка", rub_from_kopecks(metrics.get("balance_kopecks"))),
                    ("Обязательства (отдельно от остатка)", rub_from_kopecks(metrics.get("obligations_kopecks"))),
                    ("Неопознанные суммы (приход)", rub_from_kopecks(metrics.get("unclassified_in_kopecks"))),
                    ("Неопознанные суммы (расход)", rub_from_kopecks(metrics.get("unclassified_out_kopecks"))),
                ],
            )
        )
        if metrics.get("status_labels"):
            blocks.append(note(f"Метки состояния: {metrics['status_labels']}", kind="muted"))
        blocks.append(note("Отдельные операции не публикуются. Остаток — это фактически учтённые деньги в кассе и на публичных счетах, не прибыль и не свободные средства."))
        blocks.append(_categories(categories=money.categories, period_label=money.period.requested))

    blocks.append('<h2>Исторические публикации</h2>')
    blocks.append(historical_block(historical, period_label=period_label))
    blocks.append('<h2>Динамика по опубликованным периодам</h2>')
    blocks.append(
        line_chart(
            [(report.label, Decimal(report.events)) for report in history.published_reports()],
            title="Состоявшиеся мероприятия по опубликованным периодам",
            unit="мероприятий",
        )
    )
    blocks.append(
        line_chart(
            [
                (report.label, None if report.expenses is None else Decimal(report.expenses))
                for report in history.published_reports()
            ],
            title="Расходы по опубликованным периодам",
            unit="₽",
        )
    )
    blocks.append(
        note(
            "Публикации покрывают периоды разной длины, а годовой агрегат — не сумма месяцев. "
            "Пропуски не заполняются оценками.",
            kind="muted",
        )
    )
    return "".join(blocks)


def _categories(categories: Sequence[CategoryDTO], *, period_label: str) -> str:
    if not categories:
        return empty_state("Категорий за период нет.")
    rows = []
    for category in categories:
        count = "—" if category.operations_count is None else str(category.operations_count)
        rows.append(
            (
                e(category.label),
                e(category.bucket),
                rub_from_kopecks(category.amount_kopecks),
                percent_text(category.share_percent),
                count,
            )
        )
    return (
        '<h2>Поступления и расходы по категориям</h2>'
        + data_table(
            "Категории публичного контура",
            ("Категория", "Тип потока", "Сумма", "Доля", "Операций"),
            rows,
            numeric_from=2,
        )
        + note(
            "Число операций скрывается для мелких и чувствительных категорий. CSV содержит только категории и полные безопасные периоды.",
            kind="muted",
        )
        + f'<p><a class="download" href="/api/public/export/categories.csv?period={e(period_label)}">Скачать CSV категорий за {e(period_label)}</a></p>'
    )


def historical_block(historical: Mapping[str, "history.MetricView | None"], *, period_label: str) -> str:
    """The historical aggregate of a period, or the honest empty state."""

    events = historical.get("events")
    receipts = historical.get("receipts")
    expenses = historical.get("expenses")
    if all(view is None or view.value is None for view in (events, receipts, expenses)):
        return empty_state(
            "Опубликованных исторических агрегатов за этот период нет.",
            hint="Исторические суммы не распределяются по месяцам и не интерполируются.",
        )
    rows = []
    for view, label in ((events, "Мероприятий"), (receipts, "Поступления"), (expenses, "Расходы")):
        if view is None:
            rows.append((label, "нет данных", "нет данных", "нет опубликованного наблюдения"))
            continue
        value = "нет данных" if view.value is None else (f"{view.value}" if view.unit == "events" else rub_from_rubles(view.value))
        rows.append((label, value, _coverage_label(view.coverage), e(view.detail)))
    out = [data_table("Опубликованные агрегаты периода", ("Показатель", "Значение", "Покрытие", "Как получено"), rows, numeric_from=1)]
    if receipts is not None and expenses is not None and receipts.value is not None and expenses.value is not None:
        coverage = history.coverage_percent(receipts.value, expenses.value)
        difference = receipts.value - expenses.value
        out.append(
            f'<p class="coverage-inline">Покрытие расходов поступлениями: <strong>'
            f'{"не применимо" if coverage is None else f"{coverage} %"}</strong>. '
            f"Разница: <strong>{rub_from_rubles(difference)}</strong>.</p>"
        )
    if events is not None and events.source is not None:
        out.append(provenance(events.source))
    out.append(note(history.NO_BALANCE_NOTE))
    return "".join(out)


def comparisons_block(rows: Sequence[tuple[str, str]], *, period_label: str) -> str:
    """Month-over-month / year-over-year comparisons, dishonest comparisons omitted."""

    return (
        "<h2>Сравнения</h2>"
        + data_table(
            "Сравнение с прошлыми периодами (число состоявшихся мероприятий)",
            ("Сравнение", "Результат"),
            [(e(caption), e(value)) for caption, value in rows],
            numeric_from=9,
        )
        + note("Неполный период не сравнивается с полным молча: при отсутствии данных сравнение помечено как «нет данных».")
    )


def _coverage_label(coverage: str) -> str:
    return {"complete": "полное", "partial": "частичное", "unknown": "нет данных"}.get(coverage, coverage)


def events_section(events: EventsDTO | None, *, period_label: str, historical: Mapping[str, "history.MetricView | None"]) -> str:
    blocks: list[str] = ["<h1>Мероприятия</h1>"]
    if events is not None and events.public_events:
        blocks.append('<h2>Состоявшиеся публичные события</h2>')
        blocks.append(
            data_table(
                "Публичные события периода",
                ("Дата", "Тип", "Название"),
                [
                    (e(item.on.isoformat()), e(EVENT_TYPE_LABELS.get(item.type, item.type)), e(item.title))
                    for item in events.public_events
                ],
                numeric_from=9,
            )
        )
    elif events is not None:
        blocks.append(empty_state("За этот период публичных событий в календаре нет."))

    if events is not None:
        blocks.append('<h2>Статистика по типам</h2>')
        rows = [
            (e(EVENT_TYPE_LABELS.get(item.type, item.type)), str(item.count), "публичный тип" if item.public else "только агрегат")
            for item in events.event_type_details
        ]
        blocks.append(data_table("Состоявшиеся события по типам", ("Тип", "Состоялось", "Публикация"), rows, numeric_from=1))
        blocks.append(
            note(
                "Закрытые, внутренние и подготовительные события входят только в общий счётчик: ни названий, ни дат, ни точек календарной сетки."
            )
        )
        if events.calendar_granularity == "day" and events.calendar_days:
            blocks.append(calendar_grid(period_label, events.calendar_days))
        elif events.calendar_granularity == "day":
            blocks.append(empty_state("В календарной сетке этого месяца публичных событий нет."))

    blocks.append('<h2>Историческое покрытие</h2>')
    view = historical.get("events")
    if view is None or view.value is None:
        blocks.append(empty_state("Исторических данных о событиях за этот период нет."))
    else:
        blocks.append(
            f'<p>Состоялось мероприятий (опубликованный агрегат): <strong>{e(view.value)}</strong>. '
            f"Покрытие: {e(_coverage_label(view.coverage))}.</p>"
        )
        if view.source is not None:
            blocks.append(provenance(view.source))
        blocks.append(note("Разбивка исторических событий по типам в публикациях не названа, поэтому не показывается."))
    return "".join(blocks)


def calendar_grid(period_label: str, days: Mapping[str, int]) -> str:
    """A month grid of *public* events only; closed dates leave no mark at all."""

    start, _end = month_bounds(period_label)
    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(start.year, start.month)
    header = "".join(f'<th scope="col"><span aria-hidden="true">{name}</span><span class="sr-only">{full}</span></th>' for name, full in
                     (("пн", "понедельник"), ("вт", "вторник"), ("ср", "среда"), ("чт", "четверг"), ("пт", "пятница"), ("сб", "суббота"), ("вс", "воскресенье")))
    body = []
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append('<td class="day empty"></td>')
                continue
            key = f"{start.year:04d}-{start.month:02d}-{day:02d}"
            count = days.get(key)
            if count:
                cells.append(
                    f'<td class="day has-event"><span class="day-num">{day}</span>'
                    f'<span class="day-mark" aria-hidden="true">•</span>'
                    f'<span class="sr-only">публичных событий: {count}</span></td>'
                )
            else:
                cells.append(f'<td class="day"><span class="day-num">{day}</span></td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<figure class="calendar"><figcaption>Календарь публичных событий за {e(period_label)}</figcaption>'
        f'<div class="table-wrap"><table class="grid"><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
        f'<p class="hint">Точками отмечены только публичные события; дни без отметки могут содержать закрытые события.</p>'
        f"</figure>"
    )


def community_section(community: CommunityDTO | None, *, period_label: str) -> str:
    blocks: list[str] = ["<h1>Сообщество</h1>"]
    if community is None or not community.channel_points:
        blocks.append(
            empty_state(
                "Датированных точек о числе подписчиков за этот период нет.",
                hint="Подписчики не интерполируются по дням: показываются только реально полученные точки.",
            )
        )
    else:
        metrics = community.metrics
        blocks.append(
            data_table(
                "Число подписчиков канала",
                ("Дата", "Подписчиков", "Источник"),
                [(e(point.on.isoformat()), f"{point.subscribers:,}".replace(",", " "), e(point.source)) for point in community.channel_points],
                numeric_from=1,
            )
        )
        blocks.append(
            f'<p>Последний снимок: <strong>{e(metrics.get("latest_subscribers"))}</strong> на {e(metrics.get("latest_on"))}. '
            f"Изменение между фактическими точками периода: <strong>{e(_delta_text(metrics.get('change'), 'человек'))}</strong>.</p>"
        )
        if metrics.get("has_gap"):
            blocks.append(note("Между точками есть пропуски: недостающие дни не восстанавливаются.", kind="warn"))
    blocks.append(f'<p>Канал: <a href="{e(history.CHANNEL_URL)}" rel="noopener">{e(history.CHANNEL_URL)}</a></p>')
    return "".join(blocks)


def support_section(money: MoneyDTO | None, *, period_label: str, historical: Mapping[str, "history.MetricView | None"]) -> str:
    blocks: list[str] = ["<h1>Поддержка</h1>"]
    blocks.append(
        '<p>Традиция публичных отчётов сохраняется как есть: пока правила ранжирования донатеров не восстановлены '
        "по оригинальным публикациям, сайт показывает только общие показатели. Новые имена не появляются.</p>"
    )
    if money is not None:
        donation_rows = [
            (e(category.label), rub_from_kopecks(category.amount_kopecks))
            for category in money.categories
            if category.bucket in {"external_compensation", "commune_subsidy"}
        ]
        if donation_rows:
            blocks.append(data_table("Поступления поддержки по категориям", ("Категория", "Сумма"), donation_rows, numeric_from=1))
        else:
            blocks.append(empty_state("Публичных категорий поддержки за период нет."))
    receipts = historical.get("receipts")
    if receipts is not None and receipts.value is not None:
        blocks.append(
            f'<p>Исторические поступления за {e(period_label)}: <strong>{rub_from_rubles(receipts.value)}</strong> '
            f"(покрытие: {e(_coverage_label(receipts.coverage))}).</p>"
        )
        if receipts.source is not None:
            blocks.append(provenance(receipts.source))
    blocks.append(note("Число пожертвований считается как число поступлений, а не число уникальных людей. Уникальные люди по банковским комментариям не сопоставляются."))
    blocks.append(
        '<p>Как поддержать: реквизиты и ссылки берутся только из действующих публичных материалов проекта и здесь не выдумываются. '
        f'Смотрите <a href="{e(history.CHANNEL_URL)}" rel="noopener">публичный канал</a>.</p>'
    )
    return "".join(blocks)


def chronicle_section(chronicle: ChronicleDTO | None) -> str:
    blocks: list[str] = ["<h1>Летопись</h1>"]
    if chronicle is None or not chronicle.news_items:
        blocks.append(
            empty_state(
                "Записей летописи за этот период нет.",
                hint="Летопись собирается из публичных постов автоматически и не копирует анонсы.",
            )
        )
        return "".join(blocks)
    cards = []
    for item in chronicle.news_items:
        links = " · ".join(
            f'<a href="{e(safe_url(url))}" rel="noopener">источник {index + 1}</a>' for index, url in enumerate(item.source_urls)
        )
        marks = [("машинная обработка" if item.machine_processed else "без машинной обработки")]
        marks.append("проверено" if item.verified else "не проверено вручную")
        cards.append(
            '<article class="card">'
            f'<h2>{e(item.title)}</h2>'
            f'<p class="card-meta">{e(item.published_on.isoformat())} · {e(item.category)} · {e(", ".join(marks))}</p>'
            f"<p>{e(item.summary)}</p>"
            f'<p class="card-sources">{links}</p>'
            "</article>"
        )
    blocks.extend(cards)
    return "".join(blocks)


# --------------------------------------------------------------------------- #
# Reports archive (section 3.7, R01)
# --------------------------------------------------------------------------- #

def reports_index(periods: Sequence[tuple[str, str, str]], *, period: str) -> str:
    rows = []
    for start, end, grain in periods:
        if start == end:
            label = start
            url = f"/reports/{start}"
            kind = "месяц"
        elif grain == "year":
            label = f"{start[:4]}"
            url = f"/reports/{start[:4]}"
            kind = "год"
        else:
            label = f"{start}..{end}"
            url = f"/reports/{start}..{end}"
            kind = "групповой период"
        rows.append((f'<a href="{e(url)}">{e(label)}</a>', e(kind)))
    return (
        "<h1>Архив отчётов</h1>"
        "<p>Восстановленные исторические агрегаты с указанием источника и полноты. Версии отчётов доступны по постоянным ссылкам.</p>"
        + data_table("Опубликованные отчёты", ("Период", "Тип"), rows, numeric_from=9)
    )


PROVENANCE_LABELS = {
    "published": "опубликованный",
    "recomputed": "пересчитанный",
    "corrected": "исправленный",
}

SNAPSHOT_FIELD_LABELS = {
    "events": "мероприятий",
    "receipts": "поступления",
    "expenses": "расходы",
}


def _snapshot_value(field: str, value: Any) -> str:
    if value is None:
        return "нет данных"
    if field in ("receipts", "expenses"):
        return rub_from_rubles(int(value))
    return str(value)


def report_page(
    *,
    period_label: str,
    report: history.HistoricalReport | None,
    public: Mapping[str, Any],
    text: str | None = None,
    versions_rows: Sequence[Sequence[str]] = (),
    covered_by: history.HistoricalReport | None = None,
) -> str:
    blocks: list[str] = [f"<h1>Отчёт за {e(period_label)}</h1>"]
    latest = public.get("latest")
    if report is None and latest is None:
        blocks.append(
            empty_state(
                f"За период {period_label} отдельного отчёта нет.",
                hint="Месяц может быть покрыт групповым отчётом; тогда отдельный месячный отчёт не выдумывается.",
            )
        )
        if covered_by is not None:
            blocks.append(
                f'<p>Период покрыт отчётом {e(covered_by.label)}. Смотрите '
                f'<a href="/reports/{e(covered_by.start)}..{e(covered_by.end)}">групповой отчёт</a>; '
                "разбить его на отдельные месяцы нельзя.</p>"
                if covered_by.start != covered_by.end
                else f'<p>Период покрыт отчётом {e(covered_by.label)}.</p>'
            )
        return "".join(blocks)
    else:
        blocks.append('<h2>Структурированный снимок</h2>')
        source = latest or {}
        rows = [
            ("Мероприятий", str(source.get("events")) if source.get("events") is not None else "нет данных"),
            ("Поступления", rub_from_rubles(source.get("receipts"))),
            ("Расходы", rub_from_rubles(source.get("expenses"))),
            ("Происхождение", e(PROVENANCE_LABELS.get(str(source.get("provenance", "")), source.get("provenance", "—")))),
            ("Полнота", e(COMPLETENESS_LABELS.get(str(source.get("coverage", "")), source.get("coverage", "—")))),
        ]
        blocks.append(data_table("Снимок отчёта", ("Показатель", "Значение"), rows))
        if source.get("receipts") is not None and source.get("expenses") is not None:
            coverage = history.coverage_percent(source.get("receipts"), source.get("expenses"))
            blocks.append(
                f'<p>Покрытие расходов поступлениями: <strong>'
                f'{"не применимо" if coverage is None else f"{coverage} %"}</strong>.</p>'
            )
        if report is not None:
            blocks.append(provenance(report))
        blocks.append(note(history.PARAPHRASE_NOTE, kind="muted"))
        blocks.append(note(history.NO_BALANCE_NOTE))

    if public.get("changed"):
        blocks.append('<h2>Корректировки</h2>')
        for correction in public.get("corrections", ()):
            changes = ", ".join(
                f"{e(SNAPSHOT_FIELD_LABELS.get(field, field))}: "
                f"{e(_snapshot_value(field, change.get('from')))} → {e(_snapshot_value(field, change.get('to')))}"
                for field, change in correction.changes.items()
            )
            blocks.append(
                f'<div class="correction"><p><strong>Версия {correction.version}</strong> '
                f"(изменено {e(correction.at)}): {e(correction.reason)}.</p>"
                f"<p>Что изменилось: {changes}.</p></div>"
            )
        blocks.append(note("Исходная публикация не перезаписывается: изменения видны отдельной версией с причиной, без раскрытия операций."))

    if versions_rows:
        blocks.append('<h2>Версии</h2>')
        blocks.append(
            data_table(
                "Версии отчёта",
                ("Версия", "Опубликована", "Происхождение", "Ссылка"),
                [tuple(row) for row in versions_rows],
                numeric_from=1,
            )
        )

    if text:
        blocks.append('<h2>Текст отчёта</h2>')
        blocks.append(f'<pre class="report-text" tabindex="0">{e(text)}</pre>')
        blocks.append(note("Текст собран только из сохранённых агрегатов снимка; факты не досочиняются."))
    return "".join(blocks)


def methodology_section(*, period: str) -> str:
    return "".join(
        [
            "<h1>Методология</h1>",
            "<p>Проект некоммерческий по цели: публичная деятельность сообщества, коммунары покрывают затраты. "
            "Пороговые фразы не заменяют объяснение цифр.</p>",
            "<h2>Что считается чем</h2>",
            data_table(
                "Определения",
                ("Понятие", "Определение"),
                [
                    ("Компенсация", "Поступления от деятельности и донаты минус возвраты этих поступлений."),
                    ("Субсидия", "Фактически направленные коммунарами средства на содержание публичного пространства; при отсутствии источника — неизвестно."),
                    ("Расход", "Затраты публичного контура минус возвраты покупок."),
                    ("Трансферт", "Движение внутри контура: не доход и не расход, не меняет консолидированный остаток."),
                    ("Остаток", "Фактически учтённые ликвидные публичные средства на дату снимка. Не прибыль и не свободные деньги."),
                    ("Событие", "Состоявшееся, если запись есть в календаре на момент синхронизации и её конец не позже отсечки."),
                ],
                numeric_from=9,
            ),
            "<h2>Формулы</h2>",
            '<pre class="formulas" tabindex="0">E = расходы − возвраты\nC = компенсация − возвраты компенсации\n'
            "S = фактический вклад коммунаров\nD = E − C\nпокрытие = 100·C/E при E &gt; 0, иначе «не применимо»\n"
            "непокрытая часть = max(D, 0)\nпревышение = max(−D, 0)\nостаток B = сумма подтверждённых ликвидных публичных снимков</pre>",
            "<h2>Правила</h2>",
            "<ul><li>Суммы считаются целыми копейками; float в расчётах запрещён.</li>"
            "<li>Неизвестное показывается как «нет данных», подтверждённое отсутствие — как 0.</li>"
            "<li>Годовое покрытие — отношение годовых сумм, не среднее месячных процентов.</li>"
            "<li>Исторические агрегаты не распределяются по месяцам и не интерполируются.</li>"
            "<li>Закрытые события публикуются только агрегированно.</li></ul>",
            "<h2>Обновление</h2>",
            "<p>Один суточный цикл в 03:00 Europe/Moscow. Публичные страницы не обращаются к источникам.</p>",
            "<h2>Публичные источники</h2>",
            f'<p><a href="{e(history.CHANNEL_URL)}" rel="noopener">{e(history.CHANNEL_URL)}</a> — отчёты и новости. '
            f'CSV агрегатов: <a href="/api/public/export/categories.csv?period={e(period)}">категории за период</a>.</p>',
            "<h2>Версия методологии</h2>",
            "<p>Версия 1, публичный allowlist полей и периодов. Исторические и текущие данные различаются происхождением и полнотой.</p>",
        ]
    )


def home_section(
    *,
    period_label: str,
    money: MoneyDTO | None,
    events: EventsDTO | None,
    community: CommunityDTO | None,
    chronicle: ChronicleDTO | None,
    historical: Mapping[str, "history.MetricView | None"],
) -> str:
    blocks: list[str] = [
        "<h1>А дорог ли РанДом?</h1>",
        "<p>Постоянный публичный отчёт небольшого независимого культурного пространства: сколько стоит подвал, "
        "чем это покрывается и что здесь происходит. Сначала считаем, потом хвастаемся.</p>",
    ]
    receipts = historical.get("receipts")
    expenses = historical.get("expenses")
    coverage_value = None
    segments: list[tuple[str, Decimal | None]] = []
    if money is not None:
        metrics = money.metrics
        coverage_value = Decimal(metrics["coverage_percent"]) if metrics.get("coverage_percent") else None
        segments = coverage_segments(money)
    elif receipts is not None and expenses is not None and receipts.value is not None and expenses.value is not None:
        coverage_value = history.coverage_percent(receipts.value, expenses.value)
        segments = [("поступления от деятельности и донаты", coverage_value)]
    if segments:
        blocks.append(coverage_bar(segments, total_percent=coverage_value))
    else:
        blocks.append(empty_state("Показатель покрытия за этот период не рассчитан."))

    rows = []
    if money is not None:
        metrics = money.metrics
        rows.append(("Расходы", rub_from_kopecks(metrics.get("expense_kopecks"))))
        rows.append(("Поступления (компенсация)", rub_from_kopecks(metrics.get("compensation_kopecks"))))
        rows.append(("Фактический вклад коммунаров", rub_from_kopecks(metrics.get("subsidy_kopecks"))))
    rows.append(
        (
            "Исторические поступления",
            rub_from_rubles(receipts.value) if receipts is not None else "нет данных",
        )
    )
    rows.append(("Исторические расходы", rub_from_rubles(expenses.value) if expenses is not None else "нет данных"))
    blocks.append(data_table("Деньги периода", ("Показатель", "Значение"), rows, numeric_from=1))
    blocks.append(note("Непокрытая часть — не то же самое, что фактический взнос коммунаров. Остаток на счетах — показатель страницы «Деньги», а не заглавный показатель успешности."))

    events_view = historical.get("events")
    events_count = events_view.value if events_view and events_view.value is not None else None
    public_share = None
    if events is not None:
        events_count = events.metrics.get("total_events", events_count)
        public_share = events.metrics.get("public_share_percent")
    blocks.append(
        f'<p>Состоялось мероприятий: <strong>{e(events_count if events_count is not None else "нет данных")}</strong>. '
        f"Доля публичных: <strong>{e(public_share) if public_share is not None else 'нет данных'}</strong>.</p>"
    )
    blocks.append(
        "<p>Число подписчиков: "
        + (
            f"<strong>{e(community.metrics.get('latest_subscribers'))}</strong> на {e(community.metrics.get('latest_on'))}."
            if community is not None and community.channel_points
            else "<strong>нет данных</strong> — датированных точек за период нет."
        )
        + "</p>"
    )
    if chronicle is not None and chronicle.news_items:
        blocks.append('<h2>Свежие новости</h2><ul class="teasers">')
        for item in chronicle.news_items[:3]:
            blocks.append(f"<li>{e(item.title)} — {e(item.published_on.isoformat())}</li>")
        blocks.append("</ul>")
    blocks.append('<h2>Перейти</h2><ul class="links">')
    for href, label in (("/money", "Денежная статистика"), ("/events", "Календарь"), ("/support", "Поддержка"), ("/methodology", "Методология"), ("/reports", "Архив отчётов")):
        blocks.append(f'<li><a href="{e(href)}?period={e(period_label)}">{e(label)}</a></li>')
    blocks.append("</ul>")
    blocks.append('<p><a class="download" href="/api/public/export/categories.csv?period=all">Публичные агрегаты в CSV</a></p>')
    return "".join(blocks)
