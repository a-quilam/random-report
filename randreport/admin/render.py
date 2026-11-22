"""Server-side rendering of the closed editorial desk.

The desk is deliberately plain: no JavaScript at all (the CSP allows none), every
form a normal POST with a hidden CSRF field, every value escaped by
:func:`e`. Untrusted text (Telegram posts, Teamup titles, a human's comment) is
only ever rendered through ``e``, so a stored payload cannot become markup; the
editor has no rich-text field, and Markdown is exported as plain text.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape as _escape
from typing import Any, Iterable, Mapping, Sequence

from . import checks as checks_module

DESK_NAME = "Редакционный стол РанДома"

SECTIONS: tuple[tuple[str, str], ...] = (
    ("/admin/", "Обзор"),
    ("/admin/money", "Деньги"),
    ("/admin/events", "События"),
    ("/admin/channel", "Канал"),
    ("/admin/news", "Новости"),
    ("/admin/reconciliation", "Сверка"),
    ("/admin/publish", "Публикация"),
    ("/admin/settings", "Настройки"),
)

STATE_LABELS = {
    checks_module.OPERATIONAL: "оперативные",
    checks_module.VERIFIED: "проверено",
    checks_module.CONFLICT: "есть расхождение",
    checks_module.NO_DATA: "нет данных",
    checks_module.STALE: "устарели",
}

SEVERITY_LABELS = {"high": "важно", "warning": "внимание", "info": "справка"}

ISSUE_STATUS_LABELS = {"open": "открыто", "acknowledged": "известно, не устранено", "resolved": "условие исчезло"}


def e(value: Any) -> str:
    return _escape("" if value is None else str(value), quote=True)


def kopecks_text(amount: int | None) -> str:
    if amount is None:
        return "нет данных"
    rubles = (Decimal(amount) / Decimal(100)).quantize(Decimal("0.01"))
    return f"{rubles:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def state_badge(state: str) -> str:
    return f'<span class="state state-{e(state)}">{e(STATE_LABELS.get(state, state))}</span>'


def table(caption: str, headers: Sequence[str], rows: Iterable[Sequence[Any]], *, empty: str = "нет строк") -> str:
    body_rows = list(rows)
    if not body_rows:
        return f'<p class="empty">{e(empty)}</p>'
    head = "".join(f"<th scope=\"col\">{e(name)}</th>" for name in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in body_rows)
    return (
        f'<div class="table-wrap"><table><caption>{e(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def form(action: str, csrf: str, fields: Sequence[tuple[str, str, str]] = (), *, submit: str, note: str = "", confirm: bool = False) -> str:
    """A plain POST form. ``fields`` are (label, name, value) triples."""

    parts = [f'<form method="post" action="{e(action)}" class="action">']
    parts.append(f'<input type="hidden" name="csrf" value="{e(csrf)}">')
    for label, name, value in fields:
        field_id = f"f-{e(name)}-{abs(hash((action, name, value))) % 100000}"
        parts.append(f'<label for="{field_id}">{e(label)}</label>')
        parts.append(f'<input id="{field_id}" type="text" name="{e(name)}" value="{e(value)}">')
    parts.append(f'<button type="submit">{e(submit)}</button>')
    if note:
        parts.append(f'<p class="hint">{e(note)}</p>')
    parts.append("</form>")
    return "".join(parts)


def layout(*, title: str, active: str, body: str, actor: str, csrf: str, notice: str = "") -> str:
    nav = "".join(
        f'<li><a href="{e(href)}"{" aria-current=\"page\"" if href == active else ""}>{e(label)}</a></li>'
        for href, label in SECTIONS
    )
    notice_html = f'<p class="notice" role="status">{e(notice)}</p>' if notice else ""
    return (
        "<!doctype html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow, noarchive">'
        f"<title>{e(title)} · редакционный стол</title>"
        '<link rel="stylesheet" href="/admin/static/desk.css">'
        "</head><body>"
        '<a class="skip" href="#content">Перейти к содержанию</a>'
        '<header class="desk-header">'
        f"<p class=\"brand\">{e(DESK_NAME)}</p>"
        f'<nav class="sections" aria-label="Разделы стола"><ul>{nav}</ul></nav>'
        f'<p class="actor">Вошли как {e(actor)}</p>'
        f'<form method="post" action="/admin/logout" class="logout">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}"><button type="submit">Выйти</button></form>'
        "</header>"
        f"{notice_html}"
        f'<main id="content" tabindex="-1">{body}</main>'
        '<footer class="desk-footer"><p>Закрытый редакционный стол. Денежные поля только для чтения; '
        "исправление сумм — только в Zenmoney.</p></footer>"
        "</body></html>"
    )


def login_page(*, authorization_url: str | None, error: str | None = None, widget: str = "") -> str:
    error_html = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
    if authorization_url:
        button = f'<p><a class="login" href="{e(authorization_url)}">Войти через Telegram</a></p>'
    elif error:
        button = '<p><a class="login" href="/admin/login">Начать вход заново</a></p>'
    else:
        button = '<p class="error">Вход недоступен: провайдер не настроен.</p>'
    return (
        "<!doctype html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        "<title>Вход в редакционный стол</title>"
        '<link rel="stylesheet" href="/admin/static/desk.css"></head><body>'
        '<main id="content">'
        "<h1>Вход в редакционный стол</h1>"
        "<p>Стол закрыт: доступ есть только у действующих коммунаров из списка. "
        "Подписка на публичный канал доступа не даёт.</p>"
        f"{error_html}{button}{widget}"
        "</main></body></html>"
    )


def denied_page(message: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex, nofollow">'
        "<title>Доступ закрыт</title></head><body>"
        f'<main id="content"><h1>Доступ закрыт</h1><p>{e(message)}</p>'
        '<p><a href="/admin/login">Войти заново</a></p></main></body></html>'
    )


# --------------------------------------------------------------------------- #
# panels
# --------------------------------------------------------------------------- #

def overview_panel(data: Mapping[str, Any]) -> str:
    sources = table(
        "Состояние источников",
        ("Источник", "Свежесть", "Статус", "Данные на", "Полнота", "Ошибок", "Последний валидный снимок"),
        (
            (
                f'<a href="/admin/source/{e(row["source"])}">{e(row["source"])}</a>',
                e(row["freshness"]),
                e(row["status"]),
                e((row["latest"] or {}).get("last_success_at") or "нет"),
                e(row["coverage"]),
                e(row["error_count"]),
                "есть" if row["has_last_good"] else "нет",
            )
            for row in data["sources"]
        ),
        empty="Пока нет ни одного запуска источника.",
    )
    problems = table(
        "Требуют решения",
        ("Важность", "Код", "Объект", "Статус"),
        (
            (e(SEVERITY_LABELS.get(item["severity"], item["severity"])), e(item["code"]), e(item["entity_key"] or "—"), e(ISSUE_STATUS_LABELS.get(item["status"], item["status"])))
            for item in data["problems"]
        ),
        empty="Открытых проблем нет.",
    )
    unclosed = table(
        "Незакрытые периоды",
        ("Период", "Версия", "Черновик"),
        ((e(item["period"]), e(item["version"] if item["version"] is not None else "—"), e(item["draft_status"])) for item in data["unclosed_periods"]),
        empty="Все периоды с данными закрыты.",
    )
    checks_rows = table(
        "Проверенность по ревизиям",
        ("Состояние", "Число отметок"),
        ((e(STATE_LABELS.get(state, state)), e(count)) for state, count in sorted(data["checks"].items())),
    )
    release = data.get("release")
    release_html = (
        f'<p>Текущий публичный выпуск: <strong>{e(release["release_id"])}</strong>, данные на {e(release["as_of"])}, '
        f'разделы: {e(", ".join(release["sections"]))}</p>'
        if release
        else "<p>Публичный выпуск ещё не собран: публичный сайт показывает только исторический архив.</p>"
    )
    history_note = (
        f'<p>История: нерешённых наблюдений {e(data["history"]["unresolved"])}, '
        f'открытых расхождений {e(data["history"]["conflicts_open"])}, '
        f'неполные годы: {e(", ".join(data["history"]["gaps"]) or "нет")}.</p>'
    )
    return (
        f"<h1>Обзор</h1><p>Последний запуск: {e(data['last_run'] or 'нет')}. "
        f"Активных коммунаров: {e(data['members'])}. Проблем: {e(data['problem_count'])}.</p>"
        f"{release_html}{history_note}{sources}{checks_rows}{problems}{unclosed}"
    )


def money_panel(rows: Sequence[Mapping[str, Any]], *, query: Mapping[str, str]) -> str:
    body = table(
        "Исходные операции публичного контура",
        ("Дата", "Направление", "Сумма", "Категория", "Класс", "Проверка", "Аннотация", "Карточка"),
        (
            (
                e(row["occurred_on"]),
                e("расход" if row["direction"] == "out" else "поступление"),
                e(kopecks_text(row["amount_kopecks"])),
                e(row["category_label"]),
                e(row["economic_class"]),
                state_badge(row["check_state"]),
                e(row["note"] or "—"),
                f'<a href="/admin/money/{e(row["transaction_id"])}">открыть</a>',
            )
            for row in rows
        ),
        empty="В публичном контуре нет операций за выбранный фильтр.",
    )
    filters = (
        '<form method="get" action="/admin/money" role="search"><label for="q">Поиск</label>'
        f'<input id="q" name="q" value="{e(query.get("q", ""))}">'
        '<label for="p">Период</label>'
        f'<input id="p" name="p" placeholder="ГГГГ-ММ" value="{e(query.get("p", ""))}">'
        '<button type="submit">Показать</button></form>'
    )
    return (
        "<h1>Деньги</h1>"
        "<p>Показываются только операции внутри разрешённого публичного контура. Денежные поля только для чтения: "
        "сумма исправляется в Zenmoney, здесь допустимы аннотация и сопоставление категории.</p>"
        f"{filters}{body}"
    )


def money_card_panel(card: Mapping[str, Any], *, csrf: str, status: str | None = None) -> str:
    annotation_form = (
        f'<form method="post" action="/admin/actions/money/annotate" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        f'<input type="hidden" name="transaction_id" value="{e(card["transaction_id"])}">'
        f'<label for="note">Аннотация</label><input id="note" name="note" value="{e(card["note"] or "")}">'
        f'<button type="submit">Сохранить аннотацию</button></form>'
    )
    money_edit = (
        f'<form method="post" action="/admin/actions/money/edit" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        f'<input type="hidden" name="transaction_id" value="{e(card["transaction_id"])}">'
        f'<label for="amount">Изменить сумму (копейки)</label><input id="amount" name="amount" value="">'
        f'<button type="submit">Изменить сумму</button>'
        f'<p class="hint">Это действие отклоняется: денежные поля в столе только для чтения.</p></form>'
    )
    return (
        f'<h1>Операция {e(card["transaction_id"])}</h1>'
        f"<dl><dt>Дата</dt><dd>{e(card['occurred_on'])}</dd>"
        f"<dt>Сумма</dt><dd>{e(kopecks_text(card['amount_kopecks']))}</dd>"
        f"<dt>Направление</dt><dd>{e('расход' if card['direction'] == 'out' else 'поступление')}</dd>"
        f"<dt>Категория</dt><dd>{e(card['category_label'])} ({e(card['category_key'] or '—')})</dd>"
        f"<dt>Экономический класс</dt><dd>{e(card['economic_class'])}</dd>"
        f"<dt>Счёт</dt><dd>{e(card['account'] or '—')}</dd>"
        f"<dt>Проверка</dt><dd>{state_badge(card['check_state'])}</dd></dl>"
        f"{annotation_form}{money_edit}"
        f'<p><a href="/admin/money">К списку операций</a></p>'
    )


def events_panel(rows: Sequence[Mapping[str, Any]], *, csrf: str) -> str:
    body = table(
        "События источника",
        ("Начало", "Ключ", "Подкалендари", "Тип", "Публичность", "Присутствует в источнике", "Проверка"),
        (
            (
                e(row["start_utc"]),
                e(row["key"]),
                e(", ".join(row["subcalendars"])),
                e(row["event_type"]),
                e("публичное" if row["public"] else "не публикуется"),
                e("да" if row["source_present"] else "нет (архив)" if row["local_archive"] else "нет"),
                state_badge(row["check_state"]),
            )
            for row in rows
        ),
        empty="Событий пока нет.",
    )
    mapping_form = (
        '<h2>Локальное сопоставление подкалендарей</h2>'
        "<p>Правка календаря делается в Teamup; здесь меняется только локальное сопоставление типа, публичности и ролей закрытого контура. "
        "Собрания распознаются только по точному названию «Собрания» внутри разрешённого контура; подстрока собранием не считается.</p>"
        f'<form method="post" action="/admin/actions/mapping/event" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="subcalendar">Подкалендарь</label><input id="subcalendar" name="subcalendar_id">'
        '<label for="etype">Тип</label><input id="etype" name="event_type" placeholder="open|closed|internal_meeting|preparation|unknown">'
        '<label for="private">Приватность</label><input id="private" name="private" placeholder="да/нет">'
        '<label for="excluded">Исключить полностью (ни списков, ни итогов)</label>'
        '<input id="excluded" name="excluded" type="checkbox" value="1">'
        '<label for="meeting_contour">Разрешённый контур собраний</label>'
        '<input id="meeting_contour" name="meeting_contour" type="checkbox" value="1">'
        '<label for="rent">Закрытая аренда</label>'
        '<input id="rent" name="rent" type="checkbox" value="1">'
        '<button type="submit">Записать новую версию сопоставления</button></form>'
    )
    return f"<h1>События</h1>{body}{mapping_form}"


def channel_panel(rows: Sequence[Mapping[str, Any]]) -> str:
    body = table(
        "Снимки числа подписчиков",
        ("День", "Подписчики", "Источник", "Ревизия", "Получено", "Разрыв, дней", "Проверка"),
        (
            (
                e(row["on_date"]),
                e(row["subscribers"]),
                e(row["source"]),
                e(row["revision"]),
                e(row["received_at"]),
                e(row["gap_days"] if row["gap_days"] is not None else "—"),
                state_badge(row["check_state"]),
            )
            for row in rows
        ),
        empty="Снимков подписчиков пока нет.",
    )
    return (
        "<h1>Канал</h1><p>Ошибка получения не равна нулю: пропуск показывается как пропуск, "
        "ежедневной интерполяции нет.</p>" + body
    )


def news_panel(items: Sequence[Mapping[str, Any]], *, csrf: str, public_ids: Sequence[str], notice: str = "") -> str:
    rows = []
    public = set(public_ids)
    for item in items:
        revisions = "".join(
            f'<li>v{revision.revision}: {e(revision.origin)} — {e(revision.title or "")} '
            f'({e(revision.actor or "источник")})</li>'
            for revision in item["revisions"]
        )
        rows.append(
            (
                e(item["published_on"]),
                e(item["title"]),
                e(item["summary"]),
                e(item["classification"]),
                e(item["manual_state"]),
                e("публикуется" if item["news_id"] in public else "не публикуется"),
                e(", ".join(item["source_message_ids"])),
                f'<ul class="revisions">{revisions}</ul>',
                e(item["news_id"]),
            )
        )
    body = table(
        "Результаты извлечения и ручная модерация",
        ("Дата", "Заголовок", "Краткое изложение", "Классификация", "Ручное состояние", "Видимость", "Исходные сообщения", "Версии", "Ключ"),
        rows,
        empty="Новостей пока нет.",
    )
    edit_form = (
        '<h2>Исправить</h2>'
        f'<form method="post" action="/admin/actions/news/edit" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="news_id">Ключ новости</label><input id="news_id" name="news_id">'
        '<label for="title">Заголовок</label><input id="title" name="title">'
        '<label for="summary">Краткое изложение</label><input id="summary" name="summary">'
        '<label for="category">Категория</label><input id="category" name="category">'
        '<button type="submit">Сохранить как новую ручную версию</button></form>'
    )
    hide_form = (
        '<h2>Скрыть или восстановить</h2>'
        f'<form method="post" action="/admin/actions/news/hide" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="hide_id">Ключ новости</label><input id="hide_id" name="news_id">'
        '<label for="reason">Причина</label><input id="reason" name="reason">'
        '<button type="submit">Скрыть</button></form>'
        f'<form method="post" action="/admin/actions/news/restore" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="restore_id">Ключ новости</label><input id="restore_id" name="news_id">'
        '<label for="version">Версия (пусто — последняя)</label><input id="version" name="version">'
        '<button type="submit">Восстановить</button></form>'
    )
    merge_form = (
        '<h2>Объединить</h2>'
        f'<form method="post" action="/admin/actions/news/merge" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="target">Оставить</label><input id="target" name="target_id">'
        '<label for="source">Поглотить</label><input id="source" name="source_id">'
        '<label for="mreason">Причина</label><input id="mreason" name="reason">'
        '<button type="submit">Объединить по исходным сообщениям</button></form>'
    )
    notice_html = f'<p class="notice" role="status">{e(notice)}</p>' if notice else ""
    return f"<h1>Новости</h1>{notice_html}{body}{edit_form}{hide_form}{merge_form}"


def reconciliation_panel(findings: Sequence[Mapping[str, Any]], issues: Sequence[Mapping[str, Any]], *, csrf: str) -> str:
    findings_rows = table(
        "Сверка",
        ("Важность", "Что проверяем", "Объяснение", "Значимость", "Источники", "Периоды", "Объект", "Статус"),
        (
            (
                e(SEVERITY_LABELS.get(str(item["severity"]), str(item["severity"]))),
                e(item["title"]),
                e(item["explanation"]),
                e(item["significance"]),
                e(", ".join(item["sources"]) or "—"),
                e(", ".join(item["periods"]) or "—"),
                e(f"{item['entity_type'] or ''} {item['entity_key'] or ''}".strip() or "—"),
                e(item["status"]),
            )
            for item in findings
        ),
        empty="Расхождений не обнаружено.",
    )
    if issues:
        issues_rows = table(
            "Открытые проблемы в базе",
            ("Код", "Статус", "Объект", "Комментарий", "Известно"),
            (
                (
                    e(row["code"]),
                    e(ISSUE_STATUS_LABELS.get(str(row["status"]), str(row["status"]))),
                    e(row["entity_key"] or "—"),
                    e(row.get("comment") or "—"),
                    f'<form method="post" action="/admin/actions/issue/ack" class="inline">'
                    f'<input type="hidden" name="csrf" value="{e(csrf)}">'
                    f'<input type="hidden" name="issue_id" value="{e(row["issue_id"])}">'
                    f'<label class="sr" for="c-{e(row["issue_id"])}">Комментарий</label>'
                    f'<input id="c-{e(row["issue_id"])}" name="comment">'
                    f'<button type="submit">Отметить известным</button></form>',
                )
                for row in issues
            ),
        )
    else:
        issues_rows = '<p class="empty">Проблем в базе нет.</p>'
    verify_form = (
        '<h2>Отметка проверки</h2>'
        "<p>Отметка хранится по сущности и ревизии: изменение данных или сопоставления сбрасывает только затронутые отметки. "
        "Ручная проверка не закрывает расхождение.</p>"
        f'<form method="post" action="/admin/actions/verify" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="etype2">Тип сущности</label><input id="etype2" name="entity_type" placeholder="finance_transaction">'
        '<label for="ekey">Ключ</label><input id="ekey" name="entity_key">'
        '<button type="submit">Проверено</button></form>'
    )
    return f"<h1>Сверка</h1>{findings_rows}{issues_rows}{verify_form}"


def publish_panel(
    *,
    periods: Sequence[Mapping[str, Any]],
    csrf: str,
    selected: Mapping[str, Any] | None = None,
    planes: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    history_html: str = "",
) -> str:
    index_rows = table(
        "Периоды",
        ("Период", "Статус", "Зафиксированная версия", "Готовность", "Открыть"),
        (
            (
                e(item["period"]),
                e(ISSUE_STATUS_LABELS.get(str(item["status"]), str(item["status"]))),
                e(item["frozen_version"] if item.get("frozen_version") is not None else "—"),
                e("готова" if item.get("readiness", {}).get("ready") else "не готова"),
                f'<a href="/admin/publish/{e(item["period"])}">черновик</a>',
            )
            for item in periods
        ),
    )
    body = f"<h1>Публикация</h1>{index_rows}{history_html}"
    if selected is None or state is None:
        return body
    blockers = state["readiness"]["blockers"]
    blockers_html = (
        "<ul>" + "".join(f"<li>{e(item)}</li>" for item in blockers) + "</ul>" if blockers else "<p>Блокирующих замечаний нет.</p>"
    )
    plane_html = ""
    if planes:
        plane_html = (
            f'<p>Плоскость публикации: <strong>{e(selected["plane"])}</strong>. '
            f'Исторический агрегат: {e(", ".join(planes["historical"]) or "нет")}; '
            f'живой выпуск: {e(planes["live_release"] or "нет")}. {e(planes["note"])}</p>'
        )
    save_form = (
        f'<form method="post" action="/admin/actions/publish/save" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        f'<input type="hidden" name="period" value="{e(selected["period"])}">'
        f'<label for="comment">Комментарий редакции</label><input id="comment" name="comment" value="{e(selected["comment"])}">'
        f'<label for="status">Статус</label><input id="status" name="status" placeholder="draft|ready">'
        f'<button type="submit">Сохранить черновик</button></form>'
    )
    close_form = (
        f'<form method="post" action="/admin/actions/publish/close" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        f'<input type="hidden" name="period" value="{e(selected["period"])}">'
        f'<label for="close_reason">Причина закрытия</label><input id="close_reason" name="reason">'
        f'<button type="submit">Закрыть период и зафиксировать версию</button></form>'
    )
    correct_form = (
        f'<form method="post" action="/admin/actions/publish/correct" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        f'<input type="hidden" name="period" value="{e(selected["period"])}">'
        f'<label for="field">Поле</label><input id="field" name="field" placeholder="events|receipts|expenses">'
        f'<label for="value">Новое значение</label><input id="value" name="value">'
        f'<label for="creason">Причина</label><input id="creason" name="reason">'
        f'<button type="submit">Исправить новой версией</button></form>'
    )
    versions = table(
        "Версии",
        ("Версия", "Опубликована", "Ссылка"),
        (
            (e(item["version"]), e(item["published_at"]), f'<a href="/admin/publish/{e(selected["period"])}/v{e(item["version"])}">текст версии</a>')
            for item in state["versions"]
        ),
        empty="Версий пока нет.",
    )
    return (
        f"{body}"
        f'<h2>Черновик поста за {e(selected["period"])}</h2>'
        f"{plane_html}"
        f'<pre class="draft">{e(selected["text"])}</pre>'
        f'<p class="hint">Markdown для копирования</p><pre class="draft">{e(selected["markdown"])}</pre>'
        f"<h3>Готовность</h3>{blockers_html}"
        f"{save_form}{close_form}{correct_form}{versions}"
        f'<p><a href="/admin/publish/{e(selected["period"])}/export">Экспорт Markdown</a></p>'
    )


def history_section() -> str:
    return (
        "<h2>Исторический архив</h2>"
        "<p>Оригинальные публикации остаются зафиксированными. Наблюдения с неоднозначным годом, пересказом "
        "или неполным источником не становятся фактами и не входят в агрегаты.</p>"
    )


def source_detail_panel(data: Mapping[str, Any]) -> str:
    status = data["status"]
    head = (
        f"<h1>Источник {e(data['source'])}</h1>"
        f"<p>Статус: {e(status['status'])}; свежесть: {e(status['freshness'])}; "
        f"последняя попытка: {e(status['last_attempt_at'] or 'нет')}; "
        f"последний успех: {e(status['last_success_at'] or 'нет')}; данные на {e(status['data_as_of'] or 'нет')}; "
        f"полнота: {e(status['coverage'])}; ошибок: {e(status['error_count'])}.</p>"
        f"<p>{e(data['freshness_note'])}</p>"
    )
    runs = table(
        "Запуски",
        ("Дата", "Статус", "Новых", "Изменённых", "Удалённых", "Полнота", "Ошибок", "Ошибка"),
        (
            (
                e(row["logical_date"]),
                e(row["status"]),
                e(row["new_rows"]),
                e(row["changed_rows"]),
                e(row["deleted_rows"]),
                e(row["coverage"] or "—"),
                e(row["error_count"]),
                e(row["error"] or "—"),
            )
            for row in data["runs"]
        ),
        empty="Запусков не было.",
    )
    coverage = table(
        "Покрытие периодов",
        ("Тип", "С", "По", "Полнота", "Данные на", "Источник"),
        ((e(row["grain"]), e(row["from"]), e(row["to"]), e(row["coverage"]), e(row["data_as_of"] or "—"), e(row["provenance"] or "—")) for row in data["coverage"]),
        empty="Покрытие не заявлено.",
    )
    good = data["last_good"]
    last_good = (
        f"<p>Последний проверенный снимок: запуск {e(good['run_id'])}, данные на {e(good['data_as_of'])}, хранение {e(good['stored_at'])}.</p>"
        if good
        else "<p>Проверенного снимка нет: раздел пуст, а не нулевой.</p>"
    )
    issues = table(
        "Связанные проблемы",
        ("Важность", "Код", "Объект", "Статус"),
        ((e(SEVERITY_LABELS.get(row["severity"], row["severity"])), e(row["code"]), e(row["entity_key"] or "—"), e(ISSUE_STATUS_LABELS.get(row["status"], row["status"]))) for row in data["issues"]),
        empty="Проблем нет.",
    )
    return f"{head}{runs}{coverage}{last_good}{issues}<p><a href=\"/admin/\">К обзору</a></p>"


def settings_panel(view: Mapping[str, Any], audit: Sequence[Mapping[str, Any]], invalidations: Sequence[Mapping[str, Any]], *, csrf: str) -> str:
    contour = view["contour"]
    contour_html = (
        f"<p>Публичный контур {e(contour['version'])}: счета {e(', '.join(contour['public_accounts']) or 'нет')}; "
        f"личные {e(', '.join(contour['private_accounts']) or 'нет')}; карантин {e(', '.join(contour['quarantined_accounts']) or 'нет')}; "
        f"порог видимости числа операций {e(contour['count_visibility_threshold_kopecks'])} копеек.</p>"
    )
    categories = table(
        "Категории по идентификаторам",
        ("ID", "Отображаемое имя", "Экономический класс"),
        ((e(item["category_id"]), e(item["label"]), e(item["economic_class"])) for item in view["categories"]),
        empty="Сопоставление категорий ещё не задано.",
    )
    unmapped = table(
        "Категории без сопоставления",
        ("ID",),
        ((e(item),) for item in view["unmapped_categories"]),
        empty="Все встреченные категории сопоставлены.",
    )
    members_rows = table(
        "Администраторы",
        ("Telegram ID", "Активен", "Добавлен", "Заметка"),
        ((e(item["telegram_user_id"]), e("да" if item["active"] else "нет"), e(item["added_at"]), e(item["note"] or "—")) for item in view["members"]),
    )
    sources_rows = table(
        "Разрешённые источники новостей",
        ("ID", "Канал", "Включён"),
        ((e(item["source_id"]), e(item["channel_id"]), e("да" if item["enabled"] else "нет")) for item in view["news_sources"]),
        empty="Источники новостей не заданы.",
    )
    audit_rows = table(
        "Журнал изменений",
        ("Когда", "Кто", "Действие", "Объект", "Детали"),
        (
            (e(item["at"]), e(item["actor"] or "—"), e(item["action"]), e(f"{item['entity_type'] or ''} {item['entity_key'] or ''}".strip()), e(item["detail"]))
            for item in audit
        ),
        empty="Изменений пока не было.",
    )
    invalidation_rows = table(
        "Сбросы отметок проверки",
        ("Когда", "Сущность", "Причина", "Затронуто"),
        ((e(item["at"]), e(item["entity_type"]), e(item["cause"]), e(item["affected"])) for item in invalidations),
        empty="Сбросов не было.",
    )
    member_form = (
        '<h2>Изменить состав</h2>'
        "<p>Все действующие коммунары равны в правах; последнего участника удалить нельзя.</p>"
        f'<form method="post" action="/admin/actions/members/add" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="add_id">Telegram ID (добавить)</label><input id="add_id" name="telegram_user_id">'
        '<button type="submit">Добавить</button></form>'
        f'<form method="post" action="/admin/actions/members/remove" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="rm_id">Telegram ID (отозвать)</label><input id="rm_id" name="telegram_user_id">'
        '<button type="submit">Отозвать доступ и сессии</button></form>'
    )
    sources_form = (
        '<h2>Источники новостей</h2>'
        "<p>Публичные посты читаются только из разрешённых каналов; личные и коммунарские чаты не читаются.</p>"
        f'<form method="post" action="/admin/actions/news-source" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="src_id">ID источника</label><input id="src_id" name="source_id">'
        '<label for="src_channel">Канал</label><input id="src_channel" name="channel_id" placeholder="@random_culture">'
        '<label for="src_enabled">Включён</label><input id="src_enabled" name="enabled" placeholder="да/нет">'
        '<button type="submit">Сохранить источник</button></form>'
    )
    donors_form = (
        '<h2>Донатеры</h2>'
        "<p>Пока правило идентичности не восстановлено, публикуются только общие показатели и уже публично названные люди.</p>"
        f'<form method="post" action="/admin/actions/donor-alias" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="alias_id">ID псевдонима</label><input id="alias_id" name="alias_id">'
        '<label for="alias_label">Публичное имя</label><input id="alias_label" name="public_label">'
        '<label for="alias_allowed">Разрешён</label><input id="alias_allowed" name="allowed" placeholder="да/нет">'
        '<label for="alias_note">Основание</label><input id="alias_note" name="note">'
        '<button type="submit">Сохранить псевдоним</button></form>'
    )
    rollback_form = (
        '<h2>Обратимость сопоставлений</h2>'
        f'<form method="post" action="/admin/actions/mapping/rollback" class="action">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<label for="kind">Вид</label><input id="kind" name="kind" placeholder="category|event">'
        '<label for="version">Версия</label><input id="version" name="version">'
        '<button type="submit">Восстановить как новую версию</button></form>'
    )
    schedule = view["schedule"]
    return (
        "<h1>Настройки</h1>"
        f"<p>Расписание: {e(schedule['time'])} {e(schedule['timezone'])} ({e(schedule['systemd_calendar'])}). {e(schedule['note'])}</p>"
        f"<p>Правило донатеров: {e(view['donor_rule'])}</p>"
        f"{contour_html}{categories}{unmapped}{sources_rows}{sources_form}{donors_form}{members_rows}{member_form}{rollback_form}"
        f"<p>Версии сопоставления категорий: {e(', '.join(view['mapping_versions']['category']) or 'нет')}; "
        f"событий: {e(', '.join(view['mapping_versions']['event']) or 'нет')}.</p>"
        f"{invalidation_rows}{audit_rows}"
    )


def audit_panel(audit: Sequence[Mapping[str, Any]], invalidations: Sequence[Mapping[str, Any]]) -> str:
    audit_rows = table(
        "Полный журнал действий",
        ("Когда", "Кто", "Действие", "Объект", "Детали"),
        (
            (e(item["at"]), e(item["actor"] or "—"), e(item["action"]), e(f"{item['entity_type'] or ''} {item['entity_key'] or ''}".strip()), e(item["detail"]))
            for item in audit
        ),
        empty="Действий пока не было.",
    )
    invalidation_rows = table(
        "Сбросы отметок проверки",
        ("Когда", "Сущность", "Ключ", "Причина", "Правила", "Затронуто"),
        (
            (e(item["at"]), e(item["entity_type"]), e(item["entity_key"] or "—"), e(item["cause"]), e(item["rules_version"]), e(item["affected"]))
            for item in invalidations
        ),
    )
    return f"<h1>Журнал</h1>{audit_rows}{invalidation_rows}"
