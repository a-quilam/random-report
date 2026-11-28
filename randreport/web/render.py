"""Server-side rendering of the eight public pages.

Every string in this module is either a fixed label or a value taken from a
strict DTO / a historical observation. Nothing here reads a source, computes
money, or reaches for an internal id, so the rendered HTML inherits the
allowlist of the projection layer (docs/DOMAIN.md sections 8-10).

Composition. The pages are laid out as a *monthly leaflet of the basement*: a
masthead with the sheet's period and one practical line about the daily 03:00
snapshot, a flat navigation, and then ruled, form-like blocks — labelled fields
with dotted leaders, ledger rows, hairline tables — instead of a wall of cards.
Three deliberate details carry the sheet's own voice and nothing else: a small
rotated asterisk in the masthead, dotted leaders between a label and its number,
and an outlined stamp that names the state of the data (``предварительно``,
``полный период``). No slogan, no oversized hero: the first screen states the
period, the state of the data and the three numbers a reader came for.

Data rules the composition must never break:

* a partial snapshot never prints a classified total of ``0 ₽`` as if nothing
  had been spent — an unallocated amount is named as unallocated;
* a confirmed zero stays a plain ``0 ₽`` with the period's completeness stated;
* an absent snapshot becomes an explained empty state, not a column of zeros;
* the coverage bar is drawn only when a percentage actually exists, and it is
  stamped ``предварительно`` while the snapshot is not final;
* closed events leave no title, no date, no URL and no hidden field.

Accessibility contract (section 3.9, acceptance U01): one ``main`` landmark, a
skip link, keyboard-operable navigation and period switcher, focus outlines that
are never removed, WCAG-AA contrast in the stylesheet, statuses written as text
rather than colour alone, charts as inline SVG *plus* an equivalent data table,
and a layout that survives a 320 px viewport with horizontally scrollable tables.
"""

from __future__ import annotations

import calendar
from datetime import timedelta
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
from ..domain.money import format_kopecks, format_percent, share_percent
from ..domain.timeframes import month_bounds
from . import hints

SITE_NAME = "А дорог ли РанДом?"
SITE_TAGLINE = "Месячный листок подвала: сколько стоит содержание и чем оно покрыто"

NAV: tuple[tuple[str, str], ...] = (
    ("/", "Главная"),
    ("/money", "Деньги"),
    ("/events", "Мероприятия"),
    ("/community", "Сообщество"),
    ("/support", "Поддержка"),
    ("/chronicle", "Летопись"),
    ("/reports", "Архив"),
    ("/methodology", "Как считаем"),
)

EVENT_TYPE_LABELS = {
    "open": "открытые",
    "open_by_registration": "открытые по записи",
    "closed": "закрытые",
    "closed_rent": "закрытые аренды",
    "internal_meeting": "внутренние собрания",
    "meeting": "собрания",
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

#: What each snapshot state looks like on the page. Kept as one table so the
#: wording of a state cannot drift away from the state the page actually shows.
STATE_RULES: tuple[tuple[str, str], ...] = (
    ("Данные есть", "показываем число как есть, без округления в удобную сторону."),
    ("Подтверждённый ноль", "это «трат не было», а не «мы не знаем»."),
    ("Период неполон", "показываем разнесённое по категориям и отдельно неразнесённые суммы."),
    ("Данных нет", "пишем, что данных нет, и объясняем, откуда они берутся."),
    ("Снимок устарел", "оставляем последний опубликованный листок и называем его дату."),
)


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
    """Kopecks as a Russian-money string: thousands by space, kopecks by comma.

    Presentation only — the arithmetic stays in integer kopecks in the domain
    layer; this is the one place the number is dressed as ``65 089,00 ₽``.
    The rule itself lives in :mod:`randreport.web.hints`, which the allocation
    block renders through as well, so one page can never state two formats.
    """

    return hints.rub_from_kopecks(kopecks)


def rub_from_rubles(rubles: int | None) -> str:
    if rubles is None:
        return "нет данных"
    return f"{rubles:,.0f}".replace(",", " ") + " ₽"


def percent_text(value: str | Decimal | int | None) -> str:
    """A percentage exactly as the leaflet prints it: ``3,2 %`` (docs/PERCENT_FORMAT.md).

    The one display wrapper for percentages on the leaflet, the counterpart of
    :func:`rub_from_kopecks` for money: one rule — comma as the decimal separator,
    one digit, one space before ``%`` — applied wherever a percentage is *printed*.
    An unknown percentage reads ``не применимо``; it never becomes a bare zero. The
    number itself is formatted by :func:`randreport.domain.money.format_percent`, so
    a value coming from a DTO string and one computed in the renderer dress alike.
    """

    rendered = format_percent(value)
    return "не применимо" if rendered is None else f"{rendered} %"


def _percent_chart(value: str | Decimal | int | None) -> str:
    """The same rule where a coverage chart says «нет данных» for an unknown share.

    ``coverage_bar`` prints «нет данных» in its legend, its text table and its
    ``aria`` label, and keeps that wording (docs/PERCENT_FORMAT.md section 1); only
    the number's form is shared with :func:`percent_text`. Used only for the
    percentages that are *drawn* in the chart as well as printed.
    """

    rendered = format_percent(value)
    return "нет данных" if rendered is None else f"{rendered} %"


def _delta_text(value: int | None, unit: str) -> str:
    if value is None:
        return "нет данных"
    sign = "+" if value > 0 else ""
    return f"{sign}{value} {unit}"


def _thousands(value: int | None) -> str:
    if value is None:
        return "нет данных"
    return f"{value:,}".replace(",", " ")


#: Месяц в предложном падеже — для метки «событие в ноябре» из утверждённого макета.
MONTHS_IN = {
    1: "январе", 2: "феврале", 3: "марте", 4: "апреле", 5: "мае", 6: "июне",
    7: "июле", 8: "августе", 9: "сентябре", 10: "октябре", 11: "ноябре", 12: "декабре",
}


def plural(value: int | None, one: str, few: str, many: str) -> str:
    """Русская форма слова при числе: 1 событие, 2 события, 11 событий.

    The count on the poster is printed by the same rule the approved mock used, so
    a small number never reads as a mistake («1 событий»).
    """

    if value is None:
        return many
    tail = abs(int(value)) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def month_in(period_label: str) -> str | None:
    """Название месяца в предложном падеже; ``None`` для года и «всего времени»."""

    parts = period_label.split("-")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return MONTHS_IN.get(int(parts[1]))


# --------------------------------------------------------------------------- #
# Page furniture: masthead, navigation, state line, layout
# --------------------------------------------------------------------------- #

def eyebrow(text: str) -> str:
    return f'<p class="eyebrow">{e(text)}</p>'


def caption(text: str) -> str:
    return f'<p class="caption">{e(text)}</p>'


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


#: Семь разделов листка вырезками-ярлыками (макет: нижняя коллажная навигация).
#: Каждая пара — адрес, подпись и бумага вырезки; цвета ровно те, что в макете.
NAV_CUTS: tuple[tuple[str, str, str], ...] = (
    ("/money", "Деньги", "paper-yellow"),
    ("/events", "Мероприятия", "paper-news"),
    ("/community", "Сообщество", "paper-purple"),
    ("/support", "Поддержка", "paper-cream"),
    ("/chronicle", "Летопись", "paper-red"),
    ("/reports", "Архив", "paper-news"),
    ("/methodology", "Методология", "paper-sand"),
)


def nav_cuts(active: str, period: str) -> str:
    """Семь навигационных вырезок: настоящие ссылки, а не одинаковые кнопки."""

    items = []
    for href, label, paper in NAV_CUTS:
        current = ' aria-current="page"' if href == active else ""
        query = f"?period={e(period)}" if period and period != "all" else ""
        items.append(
            f'<a class="nav-cut scrap {e(paper)}" href="{e(href)}{query}"{current}>{e(label)}</a>'
        )
    return f'<nav class="site-nav" aria-label="Разделы листка">{"".join(items)}</nav>'


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
    # ``release_id`` is deliberately *not* rendered: the contract keeps the
    # machine code of a release out of the visible public strip (section 3.1/7.3).
    # It still travels with the response as the ETag and in the JSON API, which is
    # where a machine consumer looks for it. The parameter stays accepted so the
    # caller does not have to branch.
    del release_id
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
    mast_head: str = "",
) -> str:
    """Один лист бумаги: марка-вырезка, выпуск, состояние данных и полосы.

    The poster shell of the approved mock: a solid ``#F0EEE6`` sheet, a torn-paper
    brand at the top left, the run's own small print (period, freshness, sources)
    at the top right, the page's own composition in the middle and the seven
    navigation cuts before the footer. Every page is a *different page of the same
    paper*, not a copy of the home composition.
    """

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
        '<div class="poster">'
        '<header class="site-header">'
        '<div class="mast">'
        f'<a class="brand" href="/?period={e(period)}" aria-label="{e(SITE_NAME)} — на главную">'
        '<span class="star" aria-hidden="true">✳</span>'
        '<span class="scrap paper-sand"><span class="brand-line">РанДом</span><span class="brand-line">Культуры</span></span></a>'
        '<div class="mast-main">'
        f"{mast_head}"
        '<div class="sheet-top">'
        '<div class="toolrow">'
        f"{period_switcher(active, periods, period)}"
        '<p class="cadence">Обновляем раз в сутки, в 03:00 по Москве</p>'
        "</div>"
        "</div>"
        "</div>"
        "</div>"
        "</header>"
        # the run's own small print sits at the foot of the sheet, like a
        # newspaper's imprint: it never pushes the first screen's composition down
        f'<main id="content" tabindex="-1">{body}'
        f'<div class="colophon">{status}{nav(active, period)}</div>'
        f"{nav_cuts(active, period)}</main>"
        '<footer class="site-footer">'
        '<div class="foot-cols">'
        "<div>"
        '<p class="foot-lead">Публичный листок без регистрации и без операций по счетам: '
        "только суммы категорий, события и то, что уже опубликовано.</p>"
        "<p>Денежные суммы — фактические номинальные рубли, без инфляционной поправки.</p>"
        "</div>"
        "<div>"
        '<p class="foot-head">Разделы</p>'
        f'<p><a href="/money?period={e(period)}">Деньги</a> · <a href="/events?period={e(period)}">Мероприятия</a> · '
        f'<a href="/community?period={e(period)}">Сообщество</a> · <a href="/support?period={e(period)}">Поддержка</a> · '
        f'<a href="/chronicle?period={e(period)}">Летопись</a> · <a href="/reports?period={e(period)}">Архив</a> · '
        f'<a href="/methodology?period={e(period)}">Как считаем</a></p>'
        "</div>"
        "<div>"
        '<p class="foot-head">Данные и связь</p>'
        f'<p><a href="/methodology?period={e(period)}">Как считаются цифры</a> · {csv_link}</p>'
        f'<p><a href="{e(history.CHANNEL_URL)}" rel="noopener">Публичный канал</a></p>'
        "</div>"
        "</div>"
        "</footer>"
        "</div>"
        f"{scripts}"
        "</body></html>"
    )


def section_banner(kind: str, eyebrow_text: str, title: str, lead: str) -> str:
    """Начало полосы раздела: та же бумага, но своя шапка и своё главное пятно.

    ``kind`` выбирает бумагу и наклон заголовка (``sheet-money`` и так далее):
    каждая из семи страниц — отдельный лист одной газеты в том же языке, а не
    повтор геометрии главной.
    """

    paper = {
        "money": "paper-red",
        "events": "paper-yellow",
        "community": "paper-purple",
        "support": "paper-cream",
        "chronicle": "paper-news",
        "reports": "paper-sand",
        "methodology": "paper-news",
    }.get(kind, "paper-news")
    return (
        f'<header class="banner"><p class="eyebrow">{e(eyebrow_text)}</p>'
        f'<h1><span class="scrap {e(paper)}">{e(title)}</span></h1>'
        f'<p class="lead">{e(lead)}</p></header>'
    )


def empty_state(message: str, *, hint: str = "") -> str:
    hint_html = f'<p class="hint">{e(hint)}</p>' if hint else ""
    return f'<div class="empty" role="status"><p>{e(message)}</p>{hint_html}</div>'


# --------------------------------------------------------------------------- #
# Generic components
# --------------------------------------------------------------------------- #

def data_table(
    caption_text: str,
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
        f"<caption>{e(caption_text)}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody>{foot_html}</table></div>"
    )


def fields(rows: Iterable[Sequence[Any]], *, note: str = "") -> str:
    """Labelled rows with a dotted leader between the label and its number.

    This is the sheet's own shape: a filled-in form. Cells are escaped here, so
    callers pass plain text (unlike :func:`data_table`, whose callers pre-escape
    because a cell may legitimately be an anchor).
    """

    items = []
    for row in rows:
        label, value = row[0], row[1]
        tail = e(row[2]) if len(row) > 2 and row[2] else ""
        tail_html = f'<span class="field-note">{tail}</span>' if tail else ""
        items.append(
            f'<div class="field"><dt>{e(label)}{tail_html}</dt><dd class="num">{e(value)}</dd></div>'
        )
    tail_html = f'<p class="caption">{e(note)}</p>' if note else ""
    return f'<dl class="fields">{"".join(items)}</dl>{tail_html}'


def state_block(headline: str, text: str, *, kind: str = "warn") -> str:
    """An explained state of the data — never a bare dash or a silent zero."""

    return (
        f'<div class="state state-{e(kind)}" role="status">'
        f'<p class="state-head">{e(headline)}</p><p>{e(text)}</p></div>'
    )


#: Public wording for an internal flow class. The identifier itself never reaches
#: a page: it is a machine word, and the reader gets the human phrase instead.
BUCKET_LABELS = {
    "external_compensation": "поступления от деятельности и донатов",
    "commune_subsidy": "взносы коммунаров",
    "operating_expense": "содержание пространства",
    "expense_refund": "возвраты покупок",
    "compensation_refund": "возвраты по поступлениям",
    "financing_in": "финансирование",
    "financing_out": "финансирование",
    "internal_transfer": "перевод между своими счетами",
    "balance_adjustment": "корректировка учтённого остатка",
    "unclassified": "категория требует проверки",
}


def _category_cell(category: CategoryDTO) -> str:
    """A category name, its approved hint, its public flow wording, and its bar."""

    kind = BUCKET_LABELS.get(category.bucket, "вид не указан")
    return (
        f'<span class="cat-name">{e(category.label)}</span>'
        f"{hints.hint_disclosure(category.hint, label=category.label)}"
        f'<span class="cat-kind">{e(kind)}</span>'
        f"{minibar(_decimal_or_none(category.share_percent))}"
    )


def pairs(rows: Iterable[Sequence[Any]], *, caption_text: str = "") -> str:
    """A two-column explanation list: a short state on the left, its reading on the right.

    Used where a table's narrow first column would squeeze a long explanation on a
    phone — the pair list simply stacks instead.
    """

    items = "".join(
        f'<div class="pair-row"><dt>{e(row[0])}</dt><dd>{e(row[1])}</dd></div>' for row in rows
    )
    tail = f'<p class="caption">{e(caption_text)}</p>' if caption_text else ""
    return f'<dl class="pairs">{items}</dl>{tail}'


def minibar(percent: Decimal | None) -> str:
    """A small proportional bar as SVG geometry (never an inline style width).

    The site's Content-Security-Policy is ``style-src 'self'``, which drops a
    ``style`` attribute; an SVG ``width`` attribute survives it, so the bars keep
    their true proportion under the deployed policy.
    """

    if percent is None or percent <= 0:
        return '<svg class="minibar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true"></svg>'
    width = min(percent, Decimal(100))
    return (
        '<svg class="minibar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true">'
        f'<rect x="0" y="0" width="{width}" height="4"></rect></svg>'
    )


def coverage_bar(
    segments: Sequence[tuple[str, Decimal | None]],
    *,
    total_percent: Decimal | None = None,
    missing_label: str = "недостающая часть",
    state_note: str = "",
    state_kind: str = "rust",
    alt_collapsed: bool = False,
    owner_sheet: bool = False,
) -> str:
    """A 100 % coverage bar built from labelled data segments.

    Segments never become negative and the real percentage is never clipped: when
    the coverage exceeds 100 % the segments simply fill the bar and no "missing"
    part is drawn. The bar is one ``role="img"`` element with a text label, and an
    equivalent table follows it for an assistive-technology reading of the data.
    """

    described: list[str] = []
    for label, value in segments:
        described.append(f"{label}: {_percent_chart(value)}")
    headline = percent_text(total_percent)
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
    # The bar is SVG geometry: a segment width is an *attribute*, never an inline
    # ``style`` declaration. The site's Content-Security-Policy is
    # ``style-src 'self'``, which blocks a ``style`` attribute (a real browser
    # reported ``style-src-attr :: inline`` and dropped every inline width, so the
    # bar was laid out by its label text instead of its data). SVG attributes are
    # not style, so the bar keeps its true proportions under that policy.
    rects: list[str] = []
    offset = Decimal(0)
    for index, item in enumerate(drawn):
        rects.append(f'<rect class="seg seg-{index % 4}" x="{offset}" y="0" width="{item[2]}" height="10"></rect>')
        offset += Decimal(item[2])
    legend: list[str] = [
        f'<li><span class="swatch seg-{index % 4}" aria-hidden="true"></span>{e(item[0])} {percent_text(item[1])}</li>'
        for index, item in enumerate(drawn)
    ]
    if missing is not None and missing > 0:
        rects.append(f'<rect class="seg seg-missing" x="{offset}" y="0" width="{missing}" height="10"></rect>')
        legend.append(
            f'<li><span class="swatch seg-missing" aria-hidden="true"></span>{e(missing_label)} {percent_text(missing)}</li>'
        )
    label_text = f"Покрытие расходов поступлениями: {headline}. " + "; ".join(described)
    table_rows = [(label, _percent_chart(value)) for label, value in segments]
    table_rows.append(("Недостающая часть", _percent_chart(missing)))
    table_rows.append(("Итого покрытие", headline))
    marker = f' <span class="stamp stamp-{e(state_kind)}">{e(state_note)}</span>' if state_note else ""
    alt = data_table(
        "Покрытие расходов по источникам (текстовая версия графика)",
        ("Источник", "Доля"),
        [(e(label), e(value)) for label, value in table_rows],
        numeric_from=1,
        classes="chart-alt",
    )
    if alt_collapsed:
        # on the poster the chart's own legend already names every share, so the
        # numeric alternative stays one tap away instead of lengthening the sheet;
        # it is still in the document, so the chart keeps its text equivalent
        alt = (
            '<details class="chart-alt-wrap"><summary>текстовая версия графика</summary>'
            f"{alt}</details>"
        )
    if owner_sheet:
        return (f'<figure class="coverage"><div class="segments-bar"><svg class="coverage-bar" '
                f'viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="{e(label_text)}">'
                f'{"".join(rects)}</svg></div><div class="bar-scale"><span>0</span><span>100,0 %</span></div>'
                f'<details><summary>Из чего сложилось покрытие?</summary><ul class="coverage-legend">'
                f'{"".join(legend)}</ul>{alt}</details></figure>')
    return (
        f'<figure class="coverage">'
        f'<figcaption>Покрытие расходов: <strong>{e(headline)}</strong>{marker}</figcaption>'
        f'<svg class="coverage-bar" viewBox="0 0 100 10" preserveAspectRatio="none" '
        f'role="img" aria-label="{e(label_text)}">{"".join(rects)}</svg>'
        f'<ul class="coverage-legend">{"".join(legend)}</ul>'
        f"</figure>"
        + alt
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
# Money state
# --------------------------------------------------------------------------- #

ABSENT = "absent"
PARTIAL = "partial"
ZERO = "zero"
COMPLETE = "complete"

PENDING_TEXT = "не разнесено"


def money_state(money: MoneyDTO | None) -> str:
    """Which of the four money states a snapshot is in.

    ``absent``   — no snapshot at all;
    ``partial``  — known aggregates, but some amounts are not allocated to
                   categories yet, so a classified total of ``0 ₽`` would lie;
    ``zero``     — a complete period with a confirmed zero of expenses;
    ``complete`` — a complete period with a nonzero expense base.

    The distinction is the whole point: ``0 ₽`` and ``unknown`` are different
    facts, and only a final snapshot may state the first one (section 4.3).
    """

    if money is None:
        return ABSENT
    metrics = money.metrics
    unallocated = (metrics.get("unclassified_in_kopecks") or 0) + (metrics.get("unclassified_out_kopecks") or 0)
    final = metrics.get("coverage_is_final", True)
    if unallocated or final is False or money.completeness in {"partial", "unknown"}:
        return PARTIAL
    expense = metrics.get("expense_kopecks")
    if expense is None:
        return PARTIAL
    return ZERO if expense == 0 else COMPLETE


def _allocated(value: int | None, state: str) -> str:
    """A classified amount, or an honest "unallocated" while the period is partial."""

    if value is None:
        return "нет данных" if state != PARTIAL else PENDING_TEXT
    if value == 0 and state == PARTIAL:
        # A partial snapshot with an unallocated part must not print "0.00 ₽" as
        # if nothing had been spent — that reading is exactly the reported bug.
        return PENDING_TEXT
    return rub_from_kopecks(value)


def _unallocated_rows(metrics: Mapping[str, Any]) -> list[Sequence[str]]:
    rows: list[Sequence[str]] = []
    incoming = metrics.get("unclassified_in_kopecks")
    outgoing = metrics.get("unclassified_out_kopecks")
    if incoming:
        rows.append(("Неразнесённые суммы: приход", rub_from_kopecks(incoming)))
    if outgoing:
        rows.append(("Неразнесённые суммы: расход", rub_from_kopecks(outgoing)))
    return rows


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


def _coverage_of(money: MoneyDTO) -> Decimal | None:
    raw = money.metrics.get("coverage_percent")
    return Decimal(str(raw)) if raw else None


def _coverage_is_drawable(money: MoneyDTO, *, state: str) -> bool:
    """Whether a coverage bar may be drawn at all.

    No percentage, no segments, or a zero percentage while the snapshot is still
    not final: each of those would read as "расходы ничем не покрыты", which is
    not what the data says. In those cases the page refuses the diagram and
    explains itself instead.
    """

    coverage = _coverage_of(money)
    if coverage is None or not coverage_segments(money):
        return False
    return not (coverage == 0 and state == PARTIAL)


def money_coverage_block(money: MoneyDTO, *, state: str, alt_collapsed: bool = False) -> str:
    """The coverage block: a stamped bar, or an honest refusal to compute it."""

    coverage = _coverage_of(money)
    if not _coverage_is_drawable(money, state=state):
        return state_block(
            "Покрытие пока не считаем честно",
            "Часть сумм периода ещё не разнесена по категориям, поэтому процент не считается. "
            "Крупный ноль или пустая диаграмма здесь только вводили бы в заблуждение — ниже список того, что уже известно.",
        )
    assert coverage is not None  # guaranteed by _coverage_is_drawable
    partial = state == PARTIAL
    out = [
        coverage_bar(
            coverage_segments(money),
            total_percent=coverage,
            state_note="предварительно" if partial else "полный период",
            state_kind="rust" if partial else "ok",
            alt_collapsed=alt_collapsed,
        )
    ]
    rounding = " Доли в таблице округлены до десятых, поэтому их сумма может отличаться от итогового процента на одну десятую."
    if partial:
        out.append(
            f'<p class="caption">Процент посчитан только по разнесённым по категориям суммам: '
            f"неразнесённая часть в него не вошла.{rounding}</p>"
        )
    else:
        out.append(f'<p class="caption">Суммы периода разнесены по категориям полностью.{rounding}</p>')
    return "".join(out)


def money_fields(money: MoneyDTO, *, state: str) -> str:
    metrics = money.metrics
    balance = metrics.get("balance_kopecks")
    balance_text = "нет данных" if balance is None else rub_from_kopecks(balance)
    rows: list[Sequence[str]] = []
    if state == PARTIAL:
        rows.append(("Расходы, разнесённые по категориям", _allocated(metrics.get("expense_kopecks"), state)))
        rows.append(("Поступления, разнесённые по категориям", _allocated(metrics.get("compensation_kopecks"), state)))
        rows.extend(_unallocated_rows(metrics))
    else:
        if state == ZERO:
            rows.append(("Расходы за период", "0 ₽", "подтверждённых трат не было"))
        else:
            rows.append(("Расходы за период", _allocated(metrics.get("expense_kopecks"), state), "без возвратов покупок"))
        rows.append(("Поступления от деятельности и донатов", _allocated(metrics.get("compensation_kopecks"), state)))
    subsidy = metrics.get("subsidy_kopecks")
    rows.append(("Фактический вклад коммунаров (субсидия)", "пока неизвестно" if subsidy is None else rub_from_kopecks(subsidy)))
    if state not in (PARTIAL,):
        rows.append(("Разница: расходы минус поступления", rub_from_kopecks(metrics.get("uncovered_kopecks"))))
    rows.append(("Остаток на дату снимка (не прибыль)", balance_text))
    note_text = (
        "Отдельные операции не публикуются. Остаток — это учтённые деньги на публичных счетах, не свободный резерв."
    )
    if state == PARTIAL:
        note_text += " Неразнесённые суммы — публичные агрегаты, а не отдельные операции."
    return fields(rows, note=note_text)


def money_section(money: MoneyDTO | None, *, period_label: str, historical: Mapping[str, "history.MetricView | None"]) -> str:
    blocks: list[str] = [
        section_banner(
            "money",
            f"Полоса · деньги · {period_label}",
            "Деньги: расходы, поступления, остаток",
            "Из чего складывается содержание подвала и насколько его закрывают поступления: "
            "суммы категорий и публичные агрегаты — без операций и без имён.",
        ),
    ]
    if money is None:
        blocks.append(
            empty_state(
                "Текущих финансовых данных за этот период нет.",
                hint="Финансовая проекция появляется после успешного ночного сбора. Ниже — только опубликованные исторические агрегаты.",
            )
        )
    else:
        state = money_state(money)
        if state == PARTIAL:
            blocks.append(
                state_block(
                    "Часть сумм периода ещё не разнесена по категориям",
                    "Снимок неполон: «разнесено по категориям» — это не «потрачено всего». "
                    "Ноль в такой строке не значит, что расходов не было.",
                )
            )
        blocks.append(money_coverage_block(money, state=state))
        blocks.append("<h2>Основные суммы периода</h2>")
        blocks.append(money_fields(money, state=state))
        if state == PARTIAL:
            blocks.append(
                caption(
                    "Неизвестное показываем как «нет данных», подтверждённое отсутствие — как 0 ₽. Исторические агрегаты "
                    "не распределяются по месяцам и не интерполируются."
                )
            )
        blocks.append(f'<p><a href="/methodology?period={e(period_label)}">Как считаются эти числа</a> · '
                      f'<a href="/api/public/export/categories.csv?period={e(period_label)}">CSV сумм за период</a></p>')
        blocks.append(_categories(categories=money.categories, period_label=money.period.requested, metrics=money.metrics))
        allocation_html = hints.allocation_block(money.allocation)
        if allocation_html:
            blocks.append(allocation_html)

    blocks.append("<h2>Исторические публикации</h2>")
    blocks.append(historical_block(historical, period_label=period_label))
    blocks.append("<h2>Динамика по опубликованным периодам</h2>")
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
    return f'<article class="sheet sheet-money">{"".join(blocks)}</article>'


def _categories(categories: Sequence[CategoryDTO], *, period_label: str, metrics: Mapping[str, Any] | None = None) -> str:
    if not categories:
        return empty_state("Категорий за период нет.")
    metrics = metrics or {}
    incoming = [c for c in categories if c.direction == "in" and c.bucket != "balance_adjustment"]
    outgoing = [c for c in categories if c.direction == "out"]
    blocks = [
        "<h2>Поступления и расходы по категориям</h2>",
        caption(
            "Слева — что пришло, справа — на что ушло. Взносы коммунаров и возвраты покупок стоят отдельными "
            "строками: они не входят в поступления от деятельности и донатов. Полоса у названия — доля категории "
            "внутри своей стороны, а не покрытие расходов."
        ),
    ]
    totals = {
        "Поступления": (
            "Итого поступления от деятельности и донатов",
            metrics.get("compensation_kopecks"),
            "Взносы коммунаров и возвраты покупок в эту сумму не входят — это не компенсация.",
        ),
        "Расходы": (
            "Итого расходы без возвратов покупок",
            metrics.get("expense_kopecks"),
            "Строка «Возврат покупок» уменьшает итог, поэтому сумма строк выше может быть больше.",
        ),
    }
    for title, group in (("Поступления", incoming), ("Расходы", outgoing)):
        if not group:
            blocks.append(f"<h3>{e(title)}</h3>")
            blocks.append(empty_state("Категорий этого направления за период нет."))
            continue
        rows = []
        for category in group:
            count = "—" if category.operations_count is None else str(category.operations_count)
            rows.append(
                (
                    _category_cell(category),
                    rub_from_kopecks(category.amount_kopecks),
                    percent_text(category.share_percent),
                    count,
                )
            )
        blocks.append(f"<h3>{e(title)}</h3>")
        blocks.append(
            data_table(
                f"{title}: суммы публичного контура",
                ("Категория", "Сумма", "Доля", "Операций"),
                rows,
                numeric_from=1,
            )
        )
        label, value, explain = totals[title]
        if value is not None:
            blocks.append(fields([(label, rub_from_kopecks(value))], note=explain))
    blocks.append(
        note(
            "Число операций скрывается для мелких и чувствительных категорий. Категория без доли — та, для которой "
            "доля не считается, а не ноль.",
            kind="muted",
        )
    )
    blocks.append(
        f'<p><a class="download" href="/api/public/export/categories.csv?period={e(period_label)}">'
        f"Скачать CSV категорий за {e(period_label)}</a></p>"
    )
    return "".join(blocks)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a non-numeric share simply draws no bar
        return None


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
            rows.append((label, "нет данных", "нет опубликованного наблюдения"))
            continue
        value = "нет данных" if view.value is None else (f"{view.value}" if view.unit == "events" else rub_from_rubles(view.value))
        rows.append((label, value, f"покрытие: {_coverage_label(view.coverage)}; {view.detail}"))
    out = [fields(rows)]
    if receipts is not None and expenses is not None and receipts.value is not None and expenses.value is not None:
        coverage = history.coverage_percent(receipts.value, expenses.value)
        difference = receipts.value - expenses.value
        out.append(
            f'<p class="coverage-inline">Покрытие расходов поступлениями: <strong>'
            f'{percent_text(coverage)}</strong>. '
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
        + fields([(caption_text, value) for caption_text, value in rows])
        + note("Неполный период не сравнивается с полным молча: при отсутствии данных сравнение помечено как «нет данных».")
    )


def _coverage_label(coverage: str) -> str:
    return {"complete": "полное", "partial": "частичное", "unknown": "нет данных"}.get(coverage, coverage)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

def _event_rows(events: EventsDTO, *, with_links: bool) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for item in events.public_events:
        row = (
            e(item.on.isoformat()),
            e(EVENT_TYPE_LABELS.get(item.type, item.type)),
            e(item.title),
        )
        if with_links:
            row = (*row, _event_link(item))
        rows.append(row)
    return rows


def _event_link(item: PublicEventDTO) -> str:
    url = safe_url(item.announce_url)
    if not url:
        return ""
    return f'<a href="{e(url)}" rel="noopener">анонс</a>'


def events_section(events: EventsDTO | None, *, period_label: str, historical: Mapping[str, "history.MetricView | None"]) -> str:
    blocks: list[str] = [
        section_banner(
            "events",
            f"Полоса · мероприятия · {period_label}",
            "Мероприятия подвала",
            "Публичные события называем с датой и названием, собрания — только датой и постоянной "
            "подписью. Закрытые и служебные события остаются числом: ни названия, ни даты, ни точки в календаре.",
        ),
    ]

    if events is None:
        blocks.append(
            empty_state(
                "Публичного выпуска по мероприятиям за этот период нет.",
                hint="События появляются на странице после ночного сбора календаря. Ни числа, ни даты здесь не выдумываются.",
            )
        )
    else:
        metrics = events.metrics
        blocks.append(
            '<div class="strip">'
            f'<div class="cell"><p class="k-label">Всего событий</p><p class="k-num">{e(_int_or_dash(metrics.get("total_events")))}</p>'
            f'<p class="k-note">включая служебные</p></div>'
            f'<div class="cell"><p class="k-label">Публичных</p><p class="k-num">{e(_int_or_dash(metrics.get("public_events")))}</p>'
            f'<p class="k-note">{e(_share_note(metrics.get("public_share_percent")))}</p></div>'
            f'<div class="cell"><p class="k-label">Не публичных</p><p class="k-num">{e(_non_public_total(metrics))}</p>'
            f'<p class="k-note">только суммой, без деталей</p></div>'
            "</div>"
        )

    if events is not None and events.public_events:
        blocks.append("<h2>Публичные события периода</h2>")
        with_links = any(safe_url(item.announce_url) for item in events.public_events)
        headers = ("Дата", "Тип", "Название") + (("Анонс",) if with_links else ())
        blocks.append(
            data_table(
                "Публичные события периода",
                headers,
                _event_rows(events, with_links=with_links),
                numeric_from=9,
            )
        )
    elif events is not None:
        blocks.append(empty_state("За этот период публичных событий в календаре нет."))

    if events is not None and events.calendar_granularity == "day" and (events.calendar_days or events.meeting_days):
        blocks.append(calendar_grid(period_label, events.calendar_days, meetings=dict(events.meeting_days)))
    elif events is not None and events.calendar_granularity == "day":
        blocks.append(empty_state("В календарной сетке этого месяца публичных событий и собраний нет."))
    elif events is not None:
        blocks.append(
            note(
                "Календарь за этот период доступен только агрегированно: дневная сетка не рисуется, "
                "чтобы не выдавать служебные даты.",
                kind="muted",
            )
        )

    if events is not None and events.meetings:
        blocks.append("<h2>Собрания</h2>")
        blocks.append(
            data_table(
                "Состоявшиеся собрания (дата и постоянная подпись)",
                ("Дата", "Подпись"),
                [(e(item.on.isoformat()), e(item.label)) for item in events.meetings],
                numeric_from=9,
            )
        )
        blocks.append(
            note(
                "Собрания разрешены к публикации только как московская дата начала и постоянная подпись «Собрание»: "
                "исходное название, время, участники и ссылки не публикуются.",
                kind="muted",
            )
        )

    if events is not None and events.event_type_details:
        blocks.append("<h2>Статистика по типам</h2>")
        rows = [
            (e(EVENT_TYPE_LABELS.get(item.type, item.type)), str(item.count), "публичный тип" if item.public else "только агрегат")
            for item in events.event_type_details
        ]
        blocks.append(data_table("Состоявшиеся события по типам", ("Тип", "Состоялось", "Публикация"), rows, numeric_from=1))
        blocks.append(
            note(
                "Закрытые, внутренние и подготовительные события входят только в общий счётчик: ни названий, ни дат, "
                "ни точек календарной сетки. Собрания — исключение по решению владельца: только дата начала и постоянная подпись."
            )
        )

    blocks.append("<h2>Историческое покрытие</h2>")
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
    return f'<article class="sheet sheet-events">{"".join(blocks)}</article>'


def _int_or_dash(value: Any) -> str:
    return "—" if value is None else str(value)


def _share_note(share: Any) -> str:
    return f"доля {percent_text(share)}" if share is not None else "доля не посчитана"


def _non_public_total(metrics: Mapping[str, Any]) -> str:
    """Everything that is not a public event — derived, never guessed.

    The published metrics name the public count and the total; the difference is
    the only honest figure for "the rest", which is what the strip prints. A
    partial sum of a few closed buckets would silently understate it.
    """

    total = metrics.get("total_events")
    public = metrics.get("public_events")
    if isinstance(total, int) and isinstance(public, int):
        return str(max(total - public, 0))
    return "нет данных"


def calendar_grid(period_label: str, days: Mapping[str, int], *, meetings: Mapping[str, int] | None = None) -> str:
    """A month grid of public events and meetings; other closed dates leave no mark.

    Only public events and the owner-approved meetings (date plus the constant
    label) are marked — an ordinary closed date still leaves no mark at all.
    """

    meeting_days = dict(meetings or {})
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
            meeting_count = meeting_days.get(key)
            if not count and not meeting_count:
                cells.append(f'<td class="day"><span class="day-num">{day}</span></td>')
                continue
            marks = []
            described = []
            if count:
                marks.append('<span class="day-mark" aria-hidden="true">•</span>')
                described.append(f"публичных событий: {count}")
            if meeting_count:
                marks.append('<span class="day-mark day-mark-meeting" aria-hidden="true">◦</span>')
                described.append(f"собраний: {meeting_count}")
            cells.append(
                f'<td class="day has-event"><span class="day-num">{day}</span>'
                f'{"".join(marks)}<span class="sr-only">{e("; ".join(described))}</span></td>'
            )
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<figure class="calendar"><figcaption>Календарь публичных событий и собраний за {e(period_label)}</figcaption>'
        f'<div class="table-wrap"><table class="grid"><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
        f'<p class="caption">• — публичное событие, ◦ — собрание. Дни без отметки могут содержать закрытые события, '
        f"которые не раскрываются.</p>"
        f"</figure>"
    )


# --------------------------------------------------------------------------- #
# Community, support, chronicle
# --------------------------------------------------------------------------- #

def community_section(community: CommunityDTO | None, *, period_label: str) -> str:
    blocks: list[str] = [
        section_banner(
            "community",
            f"Полоса · сообщество · {period_label}",
            "Сообщество",
            "Одна честная цифра и её дата. Подписчиков не считаем по дням и не дорисовываем линию "
            "там, где есть всего одна точка.",
        ),
    ]
    if community is None or not community.channel_points:
        blocks.append(
            empty_state(
                "Датированных точек о числе подписчиков за этот период нет.",
                hint="Подписчики не интерполируются по дням: показываются только реально полученные точки.",
            )
        )
    else:
        metrics = community.metrics
        points = community.channel_points
        blocks.append(
            '<div class="strip">'
            f'<div class="cell"><p class="k-label">Подписчики канала</p>'
            f'<p class="k-num">{e(_thousands(metrics.get("latest_subscribers")))}</p>'
            f'<p class="k-note">точка на {e(metrics.get("latest_on"))}</p></div>'
            f'<div class="cell"><p class="k-label">Точек за период</p><p class="k-num">{len(points)}</p>'
            f'<p class="k-note">только реально полученные</p></div>'
            '<div class="cell"><p class="k-label">Изменение</p>'
            f'<p class="k-num">{"нет данных" if len(points) < 2 else e(_delta_text(metrics.get("change"), "чел."))}</p>'
            f'<p class="k-note">{"одна точка — прирост не считаем" if len(points) < 2 else "между фактическими точками"}</p></div>'
            "</div>"
        )
        blocks.append("<h2>Точки, из которых всё собрано</h2>")
        blocks.append(
            data_table(
                "Число подписчиков канала",
                ("Дата", "Подписчиков", "Источник"),
                [(e(point.on.isoformat()), f"{point.subscribers:,}".replace(",", " "), e(point.source)) for point in points],
                numeric_from=1,
            )
        )
        if len(points) >= 2:
            blocks.append(
                line_chart(
                    [(point.on.isoformat(), Decimal(point.subscribers)) for point in points],
                    title="Подписчики по датированным точкам",
                    unit="человек",
                )
            )
            blocks.append(note("Линия соединяет только фактические точки: промежуток между снимками не интерполируется."))
        else:
            blocks.append(note("Линия не рисуется по одной точке: соединение было бы выдумкой.", kind="muted"))
        if metrics.get("has_gap"):
            blocks.append(note("Между точками есть пропуски: недостающие дни не восстанавливаются.", kind="warn"))
    blocks.append(f'<p>Канал: <a href="{e(history.CHANNEL_URL)}" rel="noopener">{e(history.CHANNEL_URL)}</a></p>')
    return f'<article class="sheet sheet-community">{"".join(blocks)}</article>'


def support_section(money: MoneyDTO | None, *, period_label: str, historical: Mapping[str, "history.MetricView | None"]) -> str:
    blocks: list[str] = [
        section_banner(
            "support",
            f"Полоса · поддержка · {period_label}",
            "Поддержка",
            "Отчёты держатся на донатах и взносах коммунаров. Показываем только общие категории: "
            "отдельные платежи, комментарии к ним и новые имена не публикуем.",
        ),
    ]
    if money is not None:
        donation_rows = [
            (
                _category_cell(category),
                rub_from_kopecks(category.amount_kopecks),
                "—" if category.operations_count is None else str(category.operations_count),
            )
            for category in money.categories
            if category.bucket in {"external_compensation", "commune_subsidy"}
        ]
        if donation_rows:
            blocks.append("<h2>Поступления поддержки по категориям</h2>")
            blocks.append(
                data_table(
                    "Поступления поддержки по публичным категориям",
                    ("Категория", "Сумма", "Операций"),
                    donation_rows,
                    numeric_from=1,
                )
            )
        else:
            blocks.append(empty_state("Публичных категорий поддержки за период нет."))
    else:
        blocks.append(
            empty_state(
                "Публичных категорий поддержки за период нет.",
                hint="Отдельные платежи не публикуются: страница показывает только категории из публичного выпуска.",
            )
        )
    receipts = historical.get("receipts")
    if receipts is not None and receipts.value is not None:
        blocks.append(
            f'<p>Исторические поступления за {e(period_label)}: <strong>{rub_from_rubles(receipts.value)}</strong> '
            f"(покрытие: {e(_coverage_label(receipts.coverage))}).</p>"
        )
        if receipts.source is not None:
            blocks.append(provenance(receipts.source))
    blocks.append(
        note(
            "Традиция публичных отчётов сохраняется как есть: пока правила ранжирования донатеров не восстановлены "
            "по оригинальным публикациям, сайт показывает только общие показатели. Число пожертвований считается как "
            "число поступлений, а не число уникальных людей."
        )
    )
    blocks.append(
        '<p>Как поддержать: реквизиты и ссылки берём только из действующих публичных материалов проекта и здесь не выдумываем. '
        f'Смотрите <a href="{e(history.CHANNEL_URL)}" rel="noopener">публичный канал</a>.</p>'
    )
    return f'<article class="sheet sheet-support">{"".join(blocks)}</article>'


def chronicle_section(chronicle: ChronicleDTO | None) -> str:
    blocks: list[str] = [
        section_banner(
            "chronicle",
            "Полоса · летопись",
            "Летопись",
            "Короткие записи из публичных постов: что менялось в пространстве. Анонсы не пересказываем "
            "как свершившееся и ничего не досочиняем.",
        ),
    ]
    if chronicle is None or not chronicle.news_items:
        blocks.append(empty_state("Записей летописи за этот период нет."))
        blocks.append(
            field_note_rows(
                (
                    ("Как появляются записи", "Машинная обработка публичных постов, затем ссылка на оригинал."),
                    ("Чего здесь не будет", "Придуманных новостей и анонсов, выданных за состоявшееся событие."),
                )
            )
        )
        return f'<article class="sheet sheet-chronicle">{"".join(blocks)}</article>'
    entries = []
    for item in chronicle.news_items:
        links = " · ".join(
            f'<a href="{e(safe_url(url))}" rel="noopener">источник {index + 1}</a>' for index, url in enumerate(item.source_urls)
        )
        marks = ["машинная обработка" if item.machine_processed else "без машинной обработки"]
        marks.append("проверено" if item.verified else "не проверено вручную")
        entries.append(
            '<li><div class="entry-date">'
            f'<time datetime="{e(item.published_on.isoformat())}">{e(item.published_on.isoformat())}</time>'
            f'<span class="entry-cat">{e(item.category)}</span></div>'
            '<div class="entry-body">'
            f"<h2>{e(item.title)}</h2>"
            f"<p>{e(item.summary)}</p>"
            f'<p class="entry-meta">{e(" · ".join(marks))}</p>'
            f'<p class="entry-src">{links}</p>'
            "</div></li>"
        )
    blocks.append(f'<ol class="log">{"".join(entries)}</ol>')
    return f'<article class="sheet sheet-chronicle">{"".join(blocks)}</article>'


def field_note_rows(rows: Sequence[tuple[str, str]]) -> str:
    return fields(rows)


# --------------------------------------------------------------------------- #
# Reports archive (section 3.7, R01)
# --------------------------------------------------------------------------- #

def reports_index(periods: Sequence[tuple[str, str, str]], *, period: str) -> str:
    items = []
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
        # a range keeps its span: the cover font shrinks rather than breaking it
        shown = e(label)
        items.append(
            f'<li><span class="cover-top">РанДом Культуры · отчёт</span>'
            f'<a href="{e(url)}">{shown}</a>'
            f'<span class="kind">{e(kind)}</span>'
            '<span class="arrow" aria-hidden="true">→</span></li>'
        )
    banner = section_banner(
        "reports",
        "Полоса · архив",
        "Архив отчётов",
        "Плотный список выпусков по датам публикаций. Каждый ведёт на постоянную ссылку; "
        "исправления не стирают исходную версию.",
    )
    if items:
        body = banner + f'<ul class="issues">{"".join(items)}</ul>'
    else:
        body = banner + empty_state("Опубликованных отчётов пока нет.")
    return f'<article class="sheet sheet-reports">{body}</article>'


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
    blocks: list[str] = [
        section_banner(
            "reports",
            f"Полоса · архив · выпуск · {period_label}",
            f"Отчёт за {period_label}",
            "Что было опубликовано за этот период и как выпуск исправлялся. Отдельного отчёта нет — "
            "здесь об этом прямо и написано.",
        ),
    ]
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
        return f'<article class="sheet sheet-reports">{"".join(blocks)}</article>'

    blocks.append("<h2>Что было в отчёте</h2>")
    source = latest or {}
    rows = [
        ("Мероприятий", str(source.get("events")) if source.get("events") is not None else "нет данных"),
        ("Поступления", rub_from_rubles(source.get("receipts"))),
        ("Расходы", rub_from_rubles(source.get("expenses"))),
        ("Происхождение", PROVENANCE_LABELS.get(str(source.get("provenance", "")), source.get("provenance", "—"))),
        ("Полнота", COMPLETENESS_LABELS.get(str(source.get("coverage", "")), source.get("coverage", "—"))),
    ]
    blocks.append(fields(rows))
    if source.get("receipts") is not None and source.get("expenses") is not None:
        coverage = history.coverage_percent(source.get("receipts"), source.get("expenses"))
        blocks.append(
            f'<p class="coverage-inline">Покрытие расходов поступлениями: <strong>'
            f'{percent_text(coverage)}</strong>.</p>'
        )
    if report is not None:
        blocks.append(provenance(report))
    blocks.append(note(history.PARAPHRASE_NOTE, kind="muted"))
    blocks.append(note(history.NO_BALANCE_NOTE))

    if public.get("changed"):
        blocks.append("<h2>Корректировки</h2>")
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
        blocks.append("<h2>Версии</h2>")
        blocks.append(
            data_table(
                "Версии отчёта",
                ("Версия", "Опубликована", "Происхождение", "Ссылка"),
                [tuple(row) for row in versions_rows],
                numeric_from=1,
            )
        )

    if text:
        blocks.append("<h2>Текст отчёта</h2>")
        blocks.append(f'<pre class="report-text" tabindex="0">{e(text)}</pre>')
        blocks.append(note("Текст собран только из сохранённых агрегатов снимка; факты не досочиняются."))
    return f'<article class="sheet sheet-reports">{"".join(blocks)}</article>'


def methodology_section(*, period: str) -> str:
    rules = (
        "Суммы считаются целыми копейками; float в расчётах запрещён.",
        "Неизвестное показывается как «нет данных», подтверждённое отсутствие — как 0 ₽.",
        "Компенсация — поступления от деятельности и донаты минус возвраты этих поступлений.",
        "Расход — затраты публичного контура минус возвраты покупок.",
        "Трансферт между своими счетами — не доход и не расход.",
        "Остаток — учтённые ликвидные публичные средства на дату снимка, не прибыль.",
        "Годовое покрытие — отношение годовых сумм, а не среднее месячных процентов.",
        "Исторические агрегаты не распределяются по месяцам и не интерполируются.",
    )
    return '<article class="sheet sheet-methodology">' + "".join(
        [
            section_banner(
                "methodology",
                "Полоса · как считаем",
                "Без магии: правила подсчёта",
                "Восемь правил, по которым из движений по счетам получаются числа на этом сайте. "
                "Если правило не выполняется — число не показывается.",
            ),
            '<ol class="rules">' + "".join(f"<li>{e(rule)}</li>" for rule in rules) + "</ol>",
            "<h2>Что считается чем</h2>",
            pairs(
                (
                    ("Компенсация", "Поступления от деятельности и донаты минус возвраты этих поступлений."),
                    ("Субсидия", "Фактически направленные коммунарами средства на содержание публичного пространства; при отсутствии источника — неизвестно."),
                    ("Расход", "Затраты публичного контура минус возвраты покупок."),
                    ("Трансферт", "Движение внутри контура: не доход и не расход, не меняет консолидированный остаток."),
                    ("Остаток", "Фактически учтённые ликвидные публичные средства на дату снимка. Не прибыль и не свободные деньги."),
                    ("Событие", "Состоявшееся, если запись есть в календаре на момент синхронизации и её конец не позже отсечки."),
                ),
                caption_text="Термины, которыми подписаны числа на всех страницах.",
            ),
            "<h2>Формулы</h2>",
            '<pre class="formulas" tabindex="0">E = расходы − возвраты\nC = компенсация − возвраты компенсации\n'
            "S = фактический вклад коммунаров\nD = E − C\nпокрытие = 100·C/E при E &gt; 0, иначе «не применимо»\n"
            "непокрытая часть = max(D, 0)\nпревышение = max(−D, 0)\nостаток B = сумма подтверждённых ликвидных публичных снимков</pre>",
            "<h2>Как читать состояния данных</h2>",
            pairs(STATE_RULES, caption_text="Состояние данных определяет, что именно показывать на странице."),
            "<h2>Обновление</h2>",
            "<p>Один суточный цикл в 03:00 Europe/Moscow. Публичные страницы не обращаются к источникам: "
            "они читают последний опубликованный выпуск и его дату.</p>",
            "<h2>Публичные источники</h2>",
            f'<p><a href="{e(history.CHANNEL_URL)}" rel="noopener">{e(history.CHANNEL_URL)}</a> — отчёты и новости. '
            f'CSV агрегатов: <a href="/api/public/export/categories.csv?period={e(period)}">категории за период</a>.</p>',
            "<h2>О данных</h2>",
            "<p>Версия методологии 1. Публичный allowlist полей и периодов. Технические коды выпуска и служебные "
            "метки состояния доступны в машинном виде — в JSON-представлении, а не в тексте листка.</p>",
        ]
    ) + "</article>"


# --------------------------------------------------------------------------- #
# Home: the poster of the first screen
# --------------------------------------------------------------------------- #

HOME_TAGLINE = "Сколько стоит содержание подвала и чем оно покрыто."


def home_headline() -> str:
    """Главный узел главной: «А дорог ли РанДом?» из разноформатных вырезок.

    Each fragment is its own scrap of paper — a yellow square with «А», a news
    strip with the rest of the question, the red banner with the name and a torn
    corner with the question mark — exactly as the approved mock assembles them.
    The h1 keeps the whole phrase as its accessible name, so the collage is not a
    picture a screen reader has to guess at.
    """

    return (
        '<h1 class="headline" aria-label="А дорог ли РанДом?">'
        '<span class="head-top">'
        '<span class="scrap paper-yellow head-a" aria-hidden="true">А</span>'
        '<span class="scrap paper-news head-dorog" aria-hidden="true">дорог ли</span>'
        "</span>"
        '<span class="head-bottom">'
        '<span class="scrap paper-red head-random" aria-hidden="true">РанДом</span>'
        '<span class="scrap paper-news head-question" aria-hidden="true">?</span>'
        "</span></h1>"
    )


def home_mast_head(period_label: str) -> str:
    """Заголовок, тихая подпись о чём это и однострочный девиз — не слоган на пол-экрана."""

    return (
        home_headline()
        + f'<p class="eyebrow intro-line">Открытая отчётность со-общественного пространства · листок за {e(period_label)}</p>'
        + f'<p class="lead">{e(HOME_TAGLINE)}</p>'
    )


#: Разрешённое фото подвала в рабочем статическом контуре (ассет оператора, вне git).
#: Порядок задаёт предпочтение: лёгкий JPEG выигрывает у PNG, если оператор выложил оба.
PHOTO_ASSETS: tuple[str, ...] = ("basement-photo.jpg", "basement-photo.png")

#: Логотип владельца у заголовка (публичный ассет, без метаданных).
BRAND_LOGO = "brand-logo.png"

#: Подпись под настоящим фото: на этом месте в макете стояла художественная заглушка.
PHOTO_CAPTION = "Наш подвал — то самое пространство, за которое этот листок и считает."


def visual_photo(src: str, *, alt: str = "Фотография подвала") -> str:
    """Левая колонка главной с настоящим разрешённым фото: вырез с неровной кромкой.

    ``src`` arrives only when the operator has installed the approved asset in the
    served static contour — the photograph is deliberately not in git. The cut is
    the mock's own torn edge (``.visual-cut``) and the crop keeps the content of
    the frame (``object-fit: cover``), never stretching it.
    """

    return (
        '<figure class="visual has-photo" aria-label="Фотография подвала">'
        '<span class="understrip" aria-hidden="true"></span>'
        f'<img class="visual-cut photo" src="{e(src)}" alt="{e(alt)}" decoding="async">'
        f"<figcaption>{e(PHOTO_CAPTION)}</figcaption>"
        '<span class="understrip bottom" aria-hidden="true"></span>'
        "</figure>"
    )


def visual_placeholder() -> str:
    """Запасное состояние колонки: честная рама, когда разрешённого фото нет.

    The approved mock used a drawing as a stand-in. A drawing is not evidence of
    the real space, so without the owner's photograph production shows the frame,
    says what will stand there and waits for the asset to be installed.
    """

    return (
        '<figure class="visual" aria-label="Место для фотографии пространства">'
        '<span class="understrip" aria-hidden="true"></span>'
        '<div class="visual-cut" role="img" '
        'aria-label="Место под фотографию подвала: художественную заглушку макета на боевом сайте не публикуем">'
        '<span class="cut-mark" aria-hidden="true">✳</span></div>'
        "<figcaption>Фотография подвала появится здесь, когда будет разрешённый к публикации снимок. "
        "Заглушку из макета не показываем.</figcaption>"
        '<span class="understrip bottom" aria-hidden="true"></span>'
        "</figure>"
    )


def _year_of(period_label: str) -> str:
    return period_label.split("-")[0] if period_label else ""


def subscriber_delta(
    points: Sequence[Any], *, days: int = 30
) -> tuple[int | None, str | None, int | None]:
    """Прирост подписчиков за полные ``days`` суток по датированным точкам.

    Only real snapshots count: the base point must lie at least ``days`` before the
    latest one. A gap in the middle is not filled in and, when the history is
    shorter than the window, the answer is ``(None, None, None)`` — the poster then
    says so instead of drawing a growth of its own.

    The third value is the length of the interval the change actually covers. A
    dated history rarely has a point exactly ``days`` back (points arrive with the
    monthly release), so the base can be older than the window; the poster prints
    that real interval instead of labelling a rolled-over change «за 30 дней».
    """

    if len(points) < 2:
        return None, None, None
    ordered = sorted(points, key=lambda point: point.on)
    latest = ordered[-1]
    cutoff = latest.on - timedelta(days=days)
    base = None
    for point in ordered[:-1]:
        if point.on <= cutoff:
            base = point
    if base is None:
        return None, None, None
    return latest.subscribers - base.subscribers, base.on.isoformat(), (latest.on - base.on).days


def _delta_line(
    delta: int | None, base_on: str | None, *, span_days: int | None = None, days: int = 30
) -> str:
    """Строка изменения: настоящий интервал базы, а не всегда «за 30 дней»."""

    if delta is None:
        return f"прироста за {days} суток нет: истории не хватает"
    span = span_days or days
    arrow = "↗" if delta >= 0 else "↘"
    sign = "+" if delta >= 0 else "−"
    tail = f" (с {base_on})" if base_on else ""
    return f"{arrow} {sign}{_thousands(abs(delta))} за {span} {plural(span, 'день', 'дня', 'дней')}{tail}"


def _figure_scrap(paper: str, value: int | None) -> str:
    """Бумажная цифра показателя; честное «нет данных» — та же бумага, но мельче.

    A missing value is a word, not a figure: at the poster's number size «нет
    данных» is wider than its own column and ran off the sheet at every width
    (measured in a real browser), so the honest state carries a smaller cut. The
    paper, the rotation and the colour are unchanged — this is the same scrap.
    """

    shown = _thousands(value)
    wide = " wide" if value is None else ""
    return f'<span class="scrap {paper} number{wide}">{e(shown)}</span>'


def _poster_figures(
    *,
    period_label: str,
    events_current: int | None,
    events_previous: int | None,
    subscribers: int | None,
    subscribers_on: str | None,
    delta: int | None,
    delta_from: str | None,
    delta_span_days: int | None = None,
) -> str:
    """Две бумажные цифры справа: события этого месяца и подписчики сегодня."""

    month = month_in(period_label)
    if month is not None:
        events_tag = f"{plural(events_current, 'событие', 'события', 'событий')} в {month}".upper()
    else:
        events_tag = "событий за период"
    if events_previous is None:
        events_sub = "за прошлый календарный месяц данных нет"
    else:
        events_sub = f"в прошлом месяце: {_thousands(events_previous)}"
    subs_tag = plural(subscribers, "подписчик", "подписчика", "подписчиков").upper()
    if subscribers is None:
        subs_sub = "датированных точек нет"
    else:
        subs_sub = _delta_line(delta, delta_from, span_days=delta_span_days)
        if subscribers_on:
            subs_sub += f" · точка на {subscribers_on}"
    return (
        '<div class="figures">'
        '<section class="metric events" id="events" aria-labelledby="events-label">'
        f'{_figure_scrap("paper-purple", events_current)}'
        f'<span id="events-label" class="scrap paper-news tag">{e(events_tag)}</span>'
        f'<span class="subline">{e(events_sub)}</span></section>'
        '<section class="metric subs" id="community" aria-labelledby="subs-label">'
        f'{_figure_scrap("paper-yellow", subscribers)}'
        f'<span id="subs-label" class="scrap paper-news tag">{e(subs_tag)}</span>'
        f'<span class="subline">{e(subs_sub)}</span></section>'
        "</div>"
    )


def announcements_block(items: Sequence[Mapping[str, Any]] = ()) -> str:
    """«ЧТО ПЕРЕСЫЛАЛИ»: три лучших публичных анонса по числу пересылок за 30 суток.

    The counter itself is the whole point of the block, so it is never guessed: a
    card is printed only for a record that carries a real number of forwards *and*
    a real link (``safe_url`` accepted it, so a ``javascript:`` document never
    becomes a card), largest first, at most three. Chatbot history does not expose
    forwarding counters (docs/INGEST.md), so while no such records exist the block
    names the gap instead of inventing posts, ranks or numbers.
    """

    usable = [
        (item, safe_url(item.get("url")))
        for item in items
        if item.get("forwards") is not None
    ]
    with_numbers = [(item, url) for item, url in usable if url]
    ordered = sorted(with_numbers, key=lambda row: -int(row[0]["forwards"]))[:3]
    heading = (
        '<section class="trend" id="announcements" aria-labelledby="trend-title">'
        '<div class="stripe-heading"><h2 id="trend-title">Что пересылали</h2>'
        '<span class="t30">Топ-3 анонса · за последние 30 суток</span></div>'
    )
    if not ordered:
        return (
            heading
            + '<div class="announce-empty">'
            + state_block(
                "Счётчиков пересылок в публичном выпуске пока нет",
                "Источник публичных постов не отдаёт число пересылок, поэтому рейтинг анонсов не строится: "
                "ни посты, ни места, ни счётчики здесь не выдумываются. Как только счётчики появятся в разрешённых "
                "данных, блок покажет настоящий топ-3 за последние 30 суток.",
                kind="warn",
            )
            + '<p class="cut-note">Пока честнее показать пробел, чем нарисовать рейтинг.</p>'
            + "</div></section>"
        )
    cards = []
    for index, (item, url) in enumerate(ordered, start=1):
        title = str(item.get("title") or "без названия")
        forwards = _thousands(int(item["forwards"]))
        cards.append(
            f'<a class="forward" href="{e(url)}" rel="noopener">'
            f'<span class="rank">№ {index:02d}</span>'
            f'<span class="post-title">{e(title)}</span>'
            '<span class="post-bottom">'
            f'<span class="forwards">↗ {e(forwards)} пересылок</span>'
            '<span class="arrows" aria-hidden="true">↗</span></span></a>'
        )
    return heading + f'<div class="trend-grid">{"".join(cards)}</div></section>'


def _home_money_block(*, money: MoneyDTO | None, historical: Mapping[str, "history.MetricView | None"]) -> str:
    """Денежный ответ первого экрана: состояние, затем суммы, которые оно разрешает.

    The four money states stay exactly as the leaflet told them apart: an
    unallocated part is never printed as a classified ``0 ₽``, a confirmed zero
    stays a plain ``0 ₽``, an absent snapshot is an explained empty state and a
    historical aggregate carries its own caveat.
    """

    if money is None:
        receipts = historical.get("receipts")
        expenses = historical.get("expenses")
        if receipts is None and expenses is None:
            return empty_state(
                "Финансового снимка за этот период нет.",
                hint="Публикуем только то, что уже собрано ночным циклом: пустую таблицу вместо ответа не показываем.",
            )
        rows = [
            ("Поступления за опубликованный период", rub_from_rubles(receipts.value) if receipts is not None else "нет данных"),
            ("Расходы за опубликованный период", rub_from_rubles(expenses.value) if expenses is not None else "нет данных"),
            ("Остаток на дату снимка", "нет данных"),
        ]
        out = [empty_state("Текущего снимка нет — ниже только опубликованные агрегаты."), fields(rows)]
        if receipts is not None and receipts.value is not None and expenses is not None and expenses.value is not None:
            coverage = history.coverage_percent(receipts.value, expenses.value)
            out.append(
                f'<p class="coverage-inline">Покрытие расходов поступлениями: <strong>'
                f'{percent_text(coverage)}</strong> по опубликованному периоду.</p>'
            )
        out.append(note(history.NO_BALANCE_NOTE))
        return "".join(out)

    state = money_state(money)
    head = ""
    if state == PARTIAL and _coverage_is_drawable(money, state=state):
        head = state_block(
            "Покрытие предварительное: часть сумм ещё не разнесена",
            "Снимок периода неполон, поэтому процент ниже посчитан только по разнесённым по категориям суммам. "
            "Ноль в строке «разнесено по категориям» означает не «трат не было», а «ещё не распределено».",
        )
    elif state == ZERO and not _coverage_is_drawable(money, state=state):
        head = state_block(
            "Подтверждённых трат за период не было",
            "Период полный, суммы разнесены, итог расходов — ровно ноль. Это подтверждённый ноль, а не отсутствие данных.",
            kind="ok",
        )
    return head + money_coverage_block(money, state=state, alt_collapsed=True) + money_fields(money, state=state)


def home_finance(
    *,
    money: MoneyDTO | None,
    period_label: str,
    historical: Mapping[str, "history.MetricView | None"],
) -> str:
    """«ПОКРЫЛИ РАСХОДЫ»: крупный процент, полоса покрытия и газетная ведомость."""

    state = money_state(money) if money is not None else None
    coverage = _coverage_of(money) if money is not None else None
    drawable = money is not None and _coverage_is_drawable(money, state=state or "")
    if coverage is None and money is None:
        receipts = historical.get("receipts")
        expenses = historical.get("expenses")
        if receipts is not None and expenses is not None and receipts.value is not None and expenses.value is not None:
            coverage = history.coverage_percent(receipts.value, expenses.value)
    if drawable:
        percent = percent_text(coverage)
        caption = "своими поступлениями"
    elif money is None and coverage is not None:
        percent = percent_text(coverage)
        caption = "по опубликованному периоду"
    else:
        percent = "—"
        caption = "процент пока не считаем честно"
    ledger = ""
    if money is not None and state != PARTIAL:
        # a partial snapshot has no classified sum to put in the ledger: printing
        # «покрыто: 0,00 ₽» for it is exactly the lie the states exist to prevent
        compensation = money.metrics.get("compensation_kopecks")
        expense = money.metrics.get("expense_kopecks")
        if compensation is not None and expense is not None:
            ledger = (
                '<p class="fin-ledger">'
                f'<span>покрыто: <b>{e(rub_from_kopecks(compensation))}</b></span>'
                f'<span>расходы: <b>{e(rub_from_kopecks(expense))}</b></span></p>'
            )
    period_line = f"листок за {period_label}"
    if money is not None:
        period_line += f" · данные на {money.as_of.isoformat()}"
    note_line = ""
    if money is not None:
        uncovered = money.metrics.get("uncovered_kopecks")
        if state == PARTIAL:
            note_line = "Расходы периода неполны · доли в ведомости — от поступлений"
        elif uncovered is not None:
            note_line = f"Не покрыто: {rub_from_kopecks(uncovered)} · доли в ведомости — от поступлений"
    return (
        '<section class="finance" id="money" aria-label="Покрытие расходов">'
        '<header class="finance-heading"><h2>Покрыли расходы</h2>'
        f'<span class="financial-period">{e(period_line)}</span></header>'
        f'<div class="fin-top"><strong class="fin-percent">{e(percent)}</strong>'
        f'<span class="fin-caption">{e(caption)}</span></div>'
        f"{ledger}"
        + _home_money_block(money=money, historical=historical)
        + (f'<p class="financial-note">{e(note_line)}</p>' if note_line else "")
        + "</section>"
    )


def home_section(
    *,
    period_label: str,
    money: MoneyDTO | None,
    events: EventsDTO | None,
    community: CommunityDTO | None,
    chronicle: ChronicleDTO | None,
    historical: Mapping[str, "history.MetricView | None"],
    events_previous: int | None = None,
    subscribers_extra: Sequence[Any] = (),
    announcements: Sequence[Mapping[str, Any]] = (),
    photo_url: str | None = None,
) -> str:
    """Один живой плакат: заголовок, фото подвала, две бумажные цифры, бухгалтерия, анонсы.

    ``photo_url`` is the approved basement photograph when the operator has
    installed the asset in the served static contour; without it the left column
    keeps the honest frame. Where the data cannot answer, the sheet says so in
    words — an invented number here would be the exact failure the approved design
    forbids.
    """

    events_count: Any = None
    if events is not None:
        events_count = events.metrics.get("total_events")
    if events_count is None:
        view = historical.get("events")
        events_count = None if view is None else view.value
    subscribers: int | None = None
    subscribers_on: str | None = None
    points: list[Any] = []
    if community is not None:
        subscribers = community.metrics.get("latest_subscribers")
        subscribers_on = community.metrics.get("latest_on")
        points = list(community.channel_points)
    points.extend(subscribers_extra)
    delta, delta_from, delta_span = subscriber_delta(points)
    return (
        '<div id="home">'
        '<section class="mainstage" aria-label="Основные показатели листка">'
        + (visual_photo(photo_url) if photo_url else visual_placeholder())
        + '<div class="measurements">'
        + _poster_figures(
            period_label=period_label,
            events_current=events_count,
            events_previous=events_previous,
            subscribers=subscribers,
            subscribers_on=subscribers_on,
            delta=delta,
            delta_from=delta_from,
            delta_span_days=delta_span,
        )
        + home_finance(money=money, period_label=period_label, historical=historical)
        + "</div></section>"
        + announcements_block(announcements)
        + "</div>"
    )
