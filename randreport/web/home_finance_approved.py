"""The approved home finance clipping, rendered only from public aggregates."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from html import escape

from .. import history
from ..domain.dto import MoneyDTO
from ..domain.money import share_percent
from . import render


#: Именительный падеж месяца для короткой метки периода: «ноябрь», не «2026-11».
MONTH_NAMES = ('январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
               'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь')


def _short_period(period_label: str) -> str:
    """Период на листе — просто «ноябрь»; служебные даты живут в подсказке.

    A label that is not a plain month (``2025``, ``all``, a range or a test
    placeholder) is printed as it is: the short form must never invent a month.
    """

    if len(period_label) == 7 and period_label[:4].isdigit() and period_label[4] == '-':
        month = period_label[5:]
        if month.isdigit() and 1 <= int(month) <= 12:
            return MONTH_NAMES[int(month) - 1]
    return period_label


def _sources(money: MoneyDTO, total: int | None) -> list[tuple[str, int, str]]:
    """Named slices only when the public breakdown reconciles to its total.

    Each row carries its own hint: the approved ``CategoryDTO.hint`` when the
    owner wrote one, otherwise a short neutral phrase built from the confirmed
    label. The grouped «Прочее» row lists the articles it is made of, so the
    reader can always tell what the summed amount is.
    """

    categories = sorted((c for c in money.categories if c.direction == 'in'
                         and c.bucket == 'external_compensation'), key=lambda c: -c.amount_kopecks)
    if not total or any(c.amount_kopecks < 0 for c in categories) or sum(c.amount_kopecks for c in categories) != total:
        return []

    def named(category) -> tuple[str, int, str]:
        hint = (category.hint or '').strip()
        return category.label, category.amount_kopecks, hint or f'Поступления по статье «{category.label}».'

    if len(categories) <= 3:
        return [named(category) for category in categories]
    rest = categories[2:]
    return [named(category) for category in categories[:2]] + [
        ('Прочее', sum(category.amount_kopecks for category in rest),
         'В эту сумму входят: ' + ', '.join(category.label for category in rest) + '.')
    ]


def _bar(amounts: list[int], total: int, spent: int, rate_text: str) -> str:
    # Round coverage once, then award residual points to the largest remainders.
    # Above 100%, shrink all slices in proportion rather than clipping the last.
    base = max(total, spent)
    parts = [divmod(n * 100, base) for n in amounts]
    widths = [q for q, _ in parts]
    covered = (total * 200 + base) // (2 * base)
    order = sorted(range(len(parts)), key=lambda i: -parts[i][1])
    for i in order[:covered - sum(widths)]:
        widths[i] += 1
    colors = ('don', 'org', 'other') if amounts else ()
    segments = ''.join(f'<span class="{color} q{width}"></span>'
                       for color, width in zip(colors, widths))
    return (f'<div class="bar" id="bar" role="progressbar" aria-label="Покрытие расходов" '
            f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{covered}" '
            f'aria-valuetext="{rate_text}">{segments}'
            f'<span class="uncovered q{100 - covered}"></span></div>')


def _source_row(label: str, amount: int | None, share: str, hint: str) -> str:
    """One ledger row with the owner-approved hover/focus hint.

    The hint is the dark plate the owner approved (``.helpful:after``): it floats
    above the row on hover or keyboard focus, with no click and no script under the
    site's ``default-src 'none'`` policy. The torn edge of the sheet belongs to a
    pseudo-element now, so it no longer clips the first row's plate; ``tabindex``
    keeps the same text reachable from the keyboard.
    """

    return (
        f'<div class="source-row helpful" tabindex="0" data-help="{escape(hint)}">'
        f'<span>{escape(label)}</span><span class="amt">{render.rub_from_kopecks(amount)}</span>'
        f'<span class="share">{share}</span></div>'
    )


def finance(money: MoneyDTO | None, period_label: str,
            historical: Mapping[str, history.MetricView | None]) -> str:
    """Return one HTML <section id="money">; history is used only without a DTO."""
    period = _short_period(period_label)
    rows: list[tuple[str, int, str]] = []
    partial = False
    caption = 'СВОИМИ<br/>ПОСТУПЛЕНИЯМИ'
    help_text = 'Компенсирующие поступления без субсидий коммунаров / расходы пространства.'
    got_label = 'покрыто'
    if money is None:
        receipts, expenses = historical.get('receipts'), historical.get('expenses')
        total = receipts.value if receipts is not None else None
        spent = expenses.value if expenses is not None else None
        rate = history.coverage_percent(total, spent) if total is not None and spent else None
        got, expense = render.rub_from_rubles(total), render.rub_from_rubles(spent)
        note = ('Только опубликованные агрегаты; разбивки по источникам нет.'
                if receipts is not None or expenses is not None else 'Финансового снимка за этот период нет.')
        got_label = 'поступления'
        caption = 'ПО ОПУБЛИКОВАННОМУ<br/>ПЕРИОДУ'
        help_text = 'Опубликованные поступления / опубликованные расходы.'
    else:
        partial = render.money_state(money) == render.PARTIAL
        total = money.metrics.get('compensation_kopecks')
        spent = money.metrics.get('expense_kopecks')
        raw = money.metrics.get('coverage_percent')
        rate = Decimal(str(raw)) if raw is not None and spent and total is not None else None
        if partial and (not total or rate == 0):
            rate = None
        rows = _sources(money, total)
        if partial:
            rows = [row for row in rows if row[1]]
        # The plate names the month and the plaque stays one short line: a hover
        # hint the owner reads as a huge block is a defect. The service facts
        # (snapshot date, preliminary state, the denominator rule) are the long
        # methodology text and belong to the money page and the sheet's note, not
        # to the plaque.
        # The uncovered remainder is the public metric itself: an unknown value
        # reads «нет данных», never a fabricated zero.
        note = 'НЕ ПОКРЫТО: ' + render.rub_from_kopecks(money.metrics.get('uncovered_kopecks'))
        got = render.rub_from_kopecks(None if partial and total == 0 else total)
        expense = render.rub_from_kopecks(None if partial and spent == 0 else spent)

    percent = render.percent_text(rate) if rate is not None else '—'
    bar = ''
    if rate is not None and total is not None and total >= 0 and spent is not None and spent > 0:
        bar = _bar([n for _, n, _ in rows] or [total], total, spent, percent)
    entries = ''.join(
        _source_row(label, n, render.percent_text(share_percent(n, total)), hint)
        for label, n, hint in rows if total is not None
    )
    return (
        '<section class="finance" id="money" aria-label="Покрытие расходов">'
        '<header class="finance-heading"><h2>ПОКРЫЛИ РАСХОДЫ</h2>'
        f'<span class="financial-period">{escape(period)}</span></header>'
        f'<div class="fin-top"><strong class="fin-percent" id="percent">{percent}</strong>'
        f'<span class="fin-caption helpful" tabindex="0" data-help="{escape(help_text)}">{caption} '
        '<span aria-hidden="true">ⓘ</span></span></div>' + bar +
        f'<div class="fin-legend"><span>{got_label}: <b id="got">{got}</b></span>'
        f'<span>расходы: <b id="spent">{expense}</b></span></div>'
        '<div class="source-sheet" id="sources"><div class="sheet-head">'
        '<span>ОТКУДА ПОСТУПИЛО</span><span>₽ / ДОЛЯ</span></div>'
        + (entries or '<span>Нет разбивки по источникам.</span>') + '</div>'
        f'<div class="financial-note" id="finNote">{escape(note)}</div></section>'
    )
