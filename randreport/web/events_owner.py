"""Owner's approved events poster (route ``/events``).

The approved ``prototype/events.html`` (owner sign-off, 2026-11-28) is the only
source of the composition: the DOM, classes, breakpoints and interaction model
are carried over almost verbatim; only the demo generator (``makeEvents``,
``months``, demo marks and demo texts) is replaced by the allowlisted events
DTO of the nightly Teamup projection.

Privacy invariants of this page (03_DATA_AND_PRIVACY.md):

* only ``public_events`` / ``calendar_days`` / ``event_type_details`` with
  ``public=True`` and the ``metrics`` aggregates ever reach the HTML or the
  client payload — closed, internal, preparation, unknown and meeting records
  appear nowhere except as the single derived ``hidden`` sum;
* meetings are never drawn as days, dots or rows (owner: count only);
* a day after the release ``as_of`` is never presented as a held event;
* unknown values print honest absence, never ``0``;
* titles are HTML-escaped and the client payload is JSON in a
  ``type="application/json"`` script element with ``</`` escaped; announce URLs
  pass through :func:`render.safe_url` before they exist at all.

There are no inline ``style=`` attributes and no inline scripts: the poster's
CSS lives in ``/static/events.css``, its interaction in ``/static/events.js``,
and dynamic geometry (the breakdown bar widths) is SVG attributes, the same
CSP-safe pattern as the home coverage bar.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from string import Template
from typing import Any, Mapping, Sequence

from ..domain.dto import EventsDTO, PublicEventDTO
from . import render
from .render import e, safe_url

TEMPLATE = Template(Path(__file__).with_name("events_owner.html").read_text(encoding="utf-8"))

#: Russian month forms: genitive (day sheet, «25 НОЯБРЯ») and prepositional
#: (hero tag «СОБЫТИЙ В НОЯБРЕ», previous month line «в октябре»).
MONTH_GEN: Mapping[int, str] = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
MONTH_IN: Mapping[int, str] = {
    1: "январе", 2: "феврале", 3: "марте", 4: "апреле", 5: "мае", 6: "июне",
    7: "июле", 8: "августе", 9: "сентябре", 10: "октябре", 11: "ноябре", 12: "декабре",
}
#: Nominative for the period selector («Ноябрь 2026», as in the approved mock).
MONTH_NOM: Mapping[int, str] = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

#: Display names of the filter/breakdown types. Only types the DTO publishes
#: with ``public=True`` ever reach the page; the demo keys of the approved mock
#: (``lec``/``kino``/…) exist here only so the owner's visual fixture renders
#: through the production code path — ``domain.events.PUBLIC_TYPES`` can never
#: produce them from a real projection.
TYPE_LABELS: Mapping[str, str] = {
    "open": "ОТКРЫТЫЕ",
    "open_by_registration": "ОТКРЫТЫЕ ПО ЗАПИСИ",
    # fixture-only keys of the approved mock (never produced by the domain)
    "lec": "ЛЕКЦИИ",
    "kino": "КИНО",
    "igry": "ИГРЫ",
    "art": "МАСТЕРСКИЕ",
    "music": "МУЗЫКА",
}

LOG_PAGE = 8
LOG_STEP = 10


def type_label(type_key: str) -> str:
    return TYPE_LABELS.get(type_key, type_key.upper())


def month_option(label: str, selected: str) -> str:
    """One period-select option in the mock's wording («Ноябрь 2026»).

    A non-month label (a year or «all») renders as itself; the events page
    offers months, so the caller passes month labels only.
    """

    value = label
    if len(label) == 7 and label[:4].isdigit() and label[5:7].isdigit():
        text = f"{MONTH_NOM.get(int(label[5:7]), label)} {label[:4]}"
    else:
        text = label
    chosen = " selected" if label == selected else ""
    return f'<option value="{e(value)}"{chosen}>{e(text)}</option>'


def _plural_events(count: int) -> str:
    """«1 публичное событие / 2 публичных события / 5 публичных событий»."""

    if count % 100 in (11, 12, 13, 14) or count % 10 in (0, 5, 6, 7, 8, 9):
        word = "публичных событий"
    elif count % 10 == 1:
        word = "публичное событие"
    else:
        word = "публичных события"
    return f"{count} {word}"


def _int_or_dash(value: Any) -> str:
    return "—" if value is None else str(value)


#: The site-wide honest-absence wording (the money sheet prints the same words).
NO_DATA = "нет данных"


def hidden_total(events: EventsDTO | None) -> int | None:
    """Closed and internal events as one derived sum, or nothing.

    The published metrics name the total and the public count; their difference
    is the only honest figure for «the rest». A missing figure stays missing.
    """

    if events is None:
        return None
    total = events.metrics.get("total_events")
    public = events.metrics.get("public_events")
    if isinstance(total, int) and isinstance(public, int):
        return max(total - public, 0)
    return None


def public_types(events: EventsDTO | None) -> tuple[tuple[str, int], ...]:
    """The explicitly published public types, in DTO order."""

    if events is None:
        return ()
    return tuple((item.type, item.count) for item in events.event_type_details if item.public)


def _sorted_public(events: EventsDTO | None) -> list[PublicEventDTO]:
    """Published events of the period, oldest first.

    A record dated after the release ``as_of`` is never displayed even if a
    strange snapshot carried one: the page must not present a future day as
    already held (03_DATA_AND_PRIVACY.md).
    """

    if events is None:
        return []
    return [item for item in sorted(events.public_events, key=lambda item: (item.on, item.title))
            if item.on <= events.as_of]


def _announce_url(item: PublicEventDTO) -> str:
    return safe_url(item.announce_url)


def _event_view(item: PublicEventDTO) -> dict[str, Any]:
    """The public, client-safe projection of one event (payload + JS render)."""

    return {
        "d": item.on.day,
        "type": item.type,
        "title": item.title,
        "description": item.description,
        "url": _announce_url(item) or None,
    }


# --- SSR fragments (identical markup to /static/events.js renderers) -------- #


def day_grid_html(events: EventsDTO | None, *, selected_day: int, as_of: date) -> str:
    """The calendar month grid: public dots only, future days disabled."""

    year, month = _period_month(events)
    days: Mapping[str, int] = events.calendar_days if events is not None else {}
    per_day: dict[int, int] = {}
    for iso, count in days.items():
        if events is not None and date.fromisoformat(iso) > events.as_of:
            continue  # a day after the data date is never presented as held
        per_day[int(iso[8:10])] = int(count)
    first = date(year, month, 1).weekday()  # Monday-first offset (Monday=0 … Sunday=6)
    max_day = _days_in_month(year, month)
    limit = as_of.day if (as_of.year, as_of.month) == (year, month) else 0
    cells: list[str] = []
    total_cells = -(- (first + max_day) // 7) * 7
    for index in range(total_cells):
        day = index - first + 1
        if day < 1 or day > max_day:
            cells.append('<span class="day empty" aria-hidden="true"></span>')
            continue
        count = per_day.get(day, 0)
        is_future = limit > 0 and day > limit
        classes = ["day"]
        if day == selected_day:
            classes.append("active")
        if is_future:
            classes.append("outside")
        if limit > 0 and day == limit:
            classes.append("today")
        attrs = [f'class="{" ".join(classes)}"']
        if is_future:
            attrs.append("disabled")
        if day == selected_day:
            attrs.append('aria-pressed="true"')
        label = f"{day} {MONTH_GEN[month]}: {_plural_events(count)}"
        if is_future:
            label += " (вне отчётного периода)"
        attrs.append(f'aria-label="{e(label)}"')
        dots = "".join(
            f'<i class="dot {e(_day_type(events, day, index_within))}" aria-hidden="true"></i>'
            for index_within, _item in enumerate(_day_events(events, day)[:5])
        )
        cells.append(
            f'<button type="button" {" ".join(attrs)}>'
            f'<span class="day-num">{day}</span>'
            + (f'<span class="day-count">{count}</span>' if count else "")
            + f'<span class="dots">{dots}</span></button>'
        )
    return "".join(cells)


def _day_events(events: EventsDTO | None, day: int) -> list[PublicEventDTO]:
    return [item for item in _sorted_public(events) if item.on.day == day]


def _day_type(events: EventsDTO | None, day: int, index_within: int) -> str:
    items = _day_events(events, day)
    return items[index_within].type if index_within < len(items) else ""


EVENTS_NAV: tuple[tuple[str, str, str], ...] = (
    ("/", "Главная", "paper news"),
    ("/money", "Деньги", "paper yellow"),
    ("/events", "Мероприятия", "paper news"),
    ("/community", "Сообщество", "paper purple"),
    ("/support", "Поддержка", "paper sand"),
    ("/chronicle", "Летопись", "paper red"),
    ("/reports", "Архив", "paper news"),
    ("/methodology", "Методология", "paper yellow"),
)


def sheet_events_html(events: EventsDTO | None, *, selected_day: int) -> str:
    """The day sheet: only published events of the selected day (unfiltered)."""

    found = _day_events(events, selected_day)
    if not found:
        return '<div class="day-no-events">Публичных событий в этот день нет.</div>'
    parts = [f'<span class="day-label">{_plural_events(len(found)).upper()}</span>']
    for item in found:
        parts.append(
            f'<button class="day-event" type="button" data-type="{e(item.type)}">'
            f'<b>{e(item.title)}</b><span class="kind">{e(type_label(item.type))}</span></button>'
        )
    return "".join(parts)


def breakdown_html(events: EventsDTO | None) -> str:
    """The public-types histogram; a width is an SVG attribute, never a style."""

    types = public_types(events)
    total = sum(count for _key, count in types)
    rows: list[str] = []
    for key, count in types:
        pct = (round(10000 * count / total) / 100) if total else 0
        width = f"{pct:g}%"
        rows.append(
            f'<div class="bar-row"><span class="name">{e(type_label(key))}</span>'
            f'<div class="track"><svg class="fill" width="{e(width)}" height="19" aria-hidden="true" focusable="false">'
            f'<rect x="0" y="0" width="100%" height="15" class="fillrect fill-{e(key)}"/></svg></div>'
            f'<b class="value">{count}</b></div>'
        )
    if not rows:
        rows.append('<div class="bar-row"><span class="name">—</span>'
                    '<div class="track"><svg class="fill" width="0%" height="19" aria-hidden="true" focusable="false">'
                    '<rect x="0" y="0" width="0" height="15"/></svg></div><b class="value">—</b></div>')
    return "".join(rows)


def filters_html(events: EventsDTO | None) -> str:
    buttons = ['<button class="filter" data-filter="all" aria-pressed="true">ВСЕ</button>']
    for key, _count in public_types(events):
        buttons.append(
            f'<button class="filter" data-filter="{e(key)}" aria-pressed="false">{e(type_label(key))}</button>'
        )
    return "".join(buttons)


def event_log_html(events: EventsDTO | None, *, shown: int = LOG_PAGE) -> str:
    items = _sorted_public(events) if events is not None else []
    parts: list[str] = []
    for item in items[:shown]:
        parts.append(
            f'<button class="log-row" type="button" data-day="{item.on.day}">'
            f'<span class="log-date">{item.on.day:02d}</span>'
            f'<span class="log-desc"><b class="log-title">{e(item.title)}</b>'
            f'<span class="log-kind">{e(type_label(item.type))}</span></span></button>'
        )
    return "".join(parts)


def log_count_text(events: EventsDTO | None, *, filtered: bool = False) -> str:
    total = len(events.public_events) if events is not None else 0
    if not total:
        return "ПУБЛИЧНЫЕ СОБЫТИЯ МЕСЯЦА"
    suffix = "В ВЫБРАННОМ ФИЛЬТРЕ" if filtered else "МЕСЯЦА"
    return _plural_events(total).upper() + " " + suffix


# --- client payload --------------------------------------------------------- #


def page_payload(events: EventsDTO | None, *, prev_total: int | None, prev_month: int | None,
                 selected_day: int) -> str:
    """The public-only JSON the client JS needs; nothing closed can travel."""

    year, month = _period_month(events)
    if events is None:
        limit = 0
    else:
        as_of = events.as_of
        limit = as_of.day if (as_of.year, as_of.month) == (year, month) else 0
    payload: dict[str, Any] = {
        "period": f"{year:04d}-{month:02d}",
        "year": year,
        "gen": MONTH_GEN[month],
        "inMonth": MONTH_IN[month],
        "granularity": events.calendar_granularity if events is not None else "missing",
        "first_offset": date(year, month, 1).weekday(),
        "max_day": _days_in_month(year, month),
        "limit": limit,
        "total": _metric_int(events, "total_events"),
        "hidden": hidden_total(events),
        "prev": None if prev_total is None else {"monthIn": MONTH_IN.get(prev_month or 0, ""), "value": prev_total},
        "types": [{"key": key, "label": type_label(key), "count": count} for key, count in public_types(events)],
        "selectedDay": selected_day,
        "days": {},
        "events": [],
    }
    if events is not None and events.calendar_granularity == "day":
        grouped: dict[int, list[dict[str, Any]]] = {}
        for item in _sorted_public(events):
            grouped.setdefault(item.on.day, []).append(_event_view(item))
        payload["days"] = {str(day): items for day, items in sorted(grouped.items())}
        payload["events"] = [item for _day, items in sorted(grouped.items()) for item in items]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # a JSON payload inside HTML must never be able to close its own script tag
    return text.replace("</", "<\\/")


def _metric_int(events: EventsDTO | None, key: str) -> int | None:
    value = events.metrics.get(key) if events is not None else None
    return int(value) if isinstance(value, int) else None


# --- the page ---------------------------------------------------------------- #


def default_selected_day(events: EventsDTO | None, as_of_fallback: date) -> int:
    """The day the sheet opens on: the last day with published events."""

    if events is not None and events.calendar_granularity == "day" and events.calendar_days:
        last = max(int(iso[8:10]) for iso in events.calendar_days)
        return last
    year, month = _period_month(events)
    limit = as_of_fallback.day if (as_of_fallback.year, as_of_fallback.month) == (year, month) else _days_in_month(year, month)
    return min(limit, _days_in_month(year, month))


def _period_month(events: EventsDTO | None) -> tuple[int, int]:
    if events is not None:
        start = events.period.covered_start
        if start is not None:
            return start.year, start.month
        label = events.period.requested
        if len(label) == 7:
            return int(label[:4]), int(label[5:7])
    today = date.today()
    return today.year, today.month


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year + (month == 12), (month % 12) + 1, 1) - date(year, month, 1)).days


def hero_prev_html(events: EventsDTO | None, *, prev_total: int | None, prev_month: int | None) -> str:
    """«в октябре: 43» or the honest «нет сверки» — never a guessed figure."""

    if prev_total is None or prev_month is None:
        return "ПРЕДЫДУЩИЙ МЕСЯЦ: <b>НЕТ СВЕРКИ</b>"
    return f"<b>в {MONTH_IN[prev_month]}:</b> {prev_total}"


def hero_tag(events: EventsDTO | None) -> str:
    _year, month = _period_month(events)
    if events is None:
        return "ДАННЫХ ЗА ЭТОТ ПЕРИОД НЕТ"
    return f"СОБЫТИЙ В {MONTH_IN[month].upper()}"


def asof_note(events: EventsDTO | None) -> str:
    if events is None:
        return "ПУБЛИЧНЫЙ ВЫПУСК ЗА ПЕРИОД ЕЩЁ НЕ СФОРМИРОВАН"
    return f"ДАННЫЕ НА {events.as_of:%d.%m.%Y}"


def calendar_note(events: EventsDTO | None) -> str:
    """The honest caption under the grid, in the poster's own voice."""

    if events is None:
        return "Соберём календарь — покажем дни."
    if events.calendar_granularity != "day":
        return "За этот период календарь учтён только суммой: дневная сетка не публикуется."
    return ""


def foot_details(events: EventsDTO | None) -> str:
    total = _metric_int(events, "total_events")
    hidden = hidden_total(events)
    closed_part = (
        f" Из {total} событий месяца {hidden} — внутренние и закрытые: они учтены общей суммой, без дат и названий."
        if total is not None and hidden is not None else ""
    )
    return (
        "<p>Счётчик и календарь — состоявшиеся мероприятия подвала из "
        "публичного календаря Teamup, одна ночная синхронизация в 03:00 МСК." + closed_part +
        " Публичными называем открытые форматы; их даты и названия публикуются как были объявлены.</p>"
    )


def page(*, period_label: str, month_options: Sequence[str], events: EventsDTO | None,
         prev_total: int | None, prev_month: int | None, photo_url: str | None,
         logo_url: str | None) -> str:
    """The full standalone document for route ``/events`` (money-page pattern)."""

    _year, month = _period_month(events)
    selected = default_selected_day(events, events.as_of if events is not None else date.today())
    nav = "".join(
        f'<a class="{cls + " selected" if href == "/events" else cls}" '
        f'href="{e(href)}?period={e(period_label)}"'
        + (' aria-current="page"' if href == "/events" else "")
        + f">{label}</a>"
        for href, label, cls in EVENTS_NAV
    )
    brand = (
        f'<img src="{e(logo_url)}" alt="РанДом Культуры">' if logo_url else "РанДом Культуры"
    )
    photo = (
        f'<img src="{e(photo_url)}" alt="Фотография подвала РанДома во время встречи">' if photo_url else ""
    )
    log_items = _sorted_public(events) if events is not None else []
    more_hidden = "" if len(log_items) > LOG_PAGE else " hidden"
    return TEMPLATE.substitute(
        period=e(period_label),
        options="".join(month_option(label, period_label) for label in month_options),
        nav=nav,
        brand=brand,
        photo=photo,
        hero_count=e(_int_or_dash(_metric_int(events, "total_events"))),
        hero_tag=e(hero_tag(events)),
        hero_prev=hero_prev_html(events, prev_total=prev_total, prev_month=prev_month),
        hidden_count=e(NO_DATA if hidden_total(events) is None else hidden_total(events)),
        asof_note=e(asof_note(events)),
        filters=filters_html(events),
        day_grid=day_grid_html(events, selected_day=selected, as_of=events.as_of if events else date.today()),
        sheet_date=str(selected),
        sheet_month=e(MONTH_GEN[month].upper()),
        sheet_events=sheet_events_html(events, selected_day=selected),
        breakdown=breakdown_html(events),
        log_count=e(log_count_text(events)),
        event_log=event_log_html(events),
        more_hidden=more_hidden,
        payload=page_payload(events, prev_total=prev_total, prev_month=prev_month, selected_day=selected),
        calendar_note=e(calendar_note(events)),
        calendar_note_hidden="" if calendar_note(events) else " hidden",
        foot_details=foot_details(events),
    )
