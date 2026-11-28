"""Owner's standalone SSR document. Only allowlisted snapshot facts enter the paper."""
from pathlib import Path
from string import Template

from . import render
from .money_owner_data import money_owner_data
from .render import e, rub_from_kopecks as rub

TEMPLATE = Template(Path(__file__).with_name('money_owner.html').read_text(encoding='utf-8'))


def rub_compact(kopecks: int | None) -> str:
    """Poster sums in the owner's approved form: whole roubles, «67 200 ₽».

    The approved sheet never prints kopecks on the big collage figures or the
    ledger rows; the precise ``65 089,00 ₽`` leaflet convention stays in force
    inside the disclosure tables (:func:`rub_from_kopecks` below).
    """

    if kopecks is None:
        return "нет данных"
    return f"{kopecks // 100:,.0f}".replace(",", " ") + " ₽"


def row_hint(row):
    """The approved hint as a compact «?» disclosure; the mock shows no marker text."""
    if not (row.hint or '').strip():
        return ''
    return (f'<details class="hint-disclosure"><summary aria-label="Подсказка: {e(row.label)}">?</summary>'
            f'<span class="hint-text">{e(row.hint.strip())}</span></details>')


def share_note(row):
    """The owner's small grey share note, «51,5% от суммы», when a share is known."""
    if row.share_percent is None:
        return ''
    share = render.percent_text(row.share_percent).replace(' %', '%')
    if share == 'не применимо':
        return ''
    return f'<span class="cat-share">{e(share)} от суммы</span>'


def category_rows(rows, total, unclassified, partial):
    parts = []
    for row in rows:
        # the «?» mark rides on the label line, the grey share note sits under it
        label = f'<span class="cat-name">{e(row.label)}</span>{row_hint(row)}{share_note(row)}'
        parts.append(f'<div class="cat-row"><div>{label}</div><span class="cat-value">{rub(row.amount_kopecks)}</span></div>')
    if not parts and total is None:
        parts.append('<p class="cat-note">Сведений по категориям нет.</p>')
    if partial:
        # the partial state accounts for itself: what is known, what is not yet
        parts.append(f'<div class="cat-row">Разнесено по категориям<span class="cat-value">{rub(total)}</span></div>')
        parts.append(f'<div class="cat-row">Ещё не разобрано<span class="cat-value">{rub(unclassified)}</span></div>')
    elif unclassified:
        # a filled sheet with a non-zero remainder states it honestly
        parts.append(f'<div class="cat-row">Ещё не разобрано<span class="cat-value">{rub(unclassified)}</span></div>')
    return ''.join(parts)


def percent_compact(value):
    """Chart annotations in the owner's form: «91%», «54,4%» — no trailing ,0."""
    text = render.percent_text(value)          # «62,0 %»
    if text == 'не применимо':
        return text
    whole, _, decimal = text.removesuffix(' %').partition(',')
    return whole + (f',{decimal}' if decimal and decimal != '0' else '') + '%'


MONTH_NAMES = ('Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек')


def month_mark(label):
    """The approved axis lettering: the month's own name, «Июн»."""
    return MONTH_NAMES[int(label[5:7]) - 1]


def trend(months):
    """Show observations on a calendar axis. Never connect preliminary figures or gaps."""
    points = [(label, facts.coverage_percent, facts.partial or facts.coverage_is_final is not True)
              for label, facts in months if facts.coverage_percent is not None]
    if not points:
        return '<p class="trend-empty">За эти месяцы нет наблюдений покрытия расходов.</p>'
    def ordinal(label):
        year, month = map(int, label.split('-'))
        return year * 12 + month
    first, last = ordinal(points[0][0]), ordinal(points[-1][0])
    ceiling = max(100, *(p[1] for p in points))
    shapes, rows = [], []
    prev = None
    for label, value, preliminary in points:
        idx = ordinal(label)
        x = 24 + (idx-first)*740/max(last-first, 1)
        y = 160 - 140*float(value/ceiling)
        if prev and prev[0] == idx-1 and not (prev[3] or preliminary):
            shapes.append(f'<line x1="{prev[1]}" y1="{prev[2]}" x2="{x}" y2="{y}" stroke="currentColor" stroke-width="4"/>')
        shapes.append(f'<text x="{x}" y="182" text-anchor="middle" font-size="16">{month_mark(label)}</text>')
        shapes.append(f'<text x="{x}" y="{y-10}" text-anchor="middle" font-size="15">{percent_compact(value)}</text>')
        shapes.append(f'<circle cx="{x}" cy="{y}" r="7" fill="#B22624"/>')
        prev = idx, x, y, preliminary
        suffix = ' · предварительно' if preliminary else ''
        rows.append(f'<li><a href="/money?period={label}">{label}</a>: {render.percent_text(value)}{suffix}</li>')
    # The approved sheet draws a dashed 100 % guide above the plot; it only means
    # something when the axis tops out exactly at one hundred percent.
    ceiling_line = '<path d="M20 20H768" stroke="#B8AB9F" stroke-dasharray="4 5" fill="none"/>' if ceiling == 100 else ''
    # the approved sheet shows only the plot; the month list stays in the
    # document for assistive technology (and keeps the period links)
    return ('<svg viewBox="-12 -10 820 205" aria-hidden="true"><path d="M20 162H768" stroke="#B8AB9F"/>'
            + ceiling_line + ''.join(shapes) + '</svg><div class="visually-hidden" inert><details><summary>текстовая версия графика</summary><ul>'
            + ''.join(rows) + '</ul></details></div>')


def page(*, period, periods, money, months, status, historical, photo_url, logo_url):
    facts = money_owner_data(money)
    pct = facts.coverage_percent
    partial = facts.partial or (pct is not None and facts.coverage_is_final is not True)
    stamp, caution, footnote = 'ПЕРИОД РАЗНЕСЁН', '', 'Субсидии коммунаров в поступления не включены: иначе самоокупаемость будет нарисованной.'
    if not facts.available:
        stamp, caution = 'ПОКА НЕТ ДАННЫХ', 'Текущих финансовых данных за этот период нет.'
        footnote = 'Отсутствие данных не означает нулевых расходов.'
    elif partial:
        stamp, caution = 'ПРЕДВАРИТЕЛЬНО', 'Снимок неполон: известные суммы и покрытие — не итог периода.'
        footnote = 'Разнесённые суммы — ещё не вся кассовая история. Неразнесённые операции показаны ниже, без раскрытия переводов.'
    elif facts.expense_kopecks == 0:
        stamp, caution = 'РАСХОДОВ НЕ БЫЛО', 'При нулевых расходах процент не рассчитывается.'
    coverage, bar = '—', ''
    if pct is not None and not (pct == 0 and partial):
        formatted = render.percent_text(pct)
        whole, _, decimal = formatted.removesuffix(' %').partition(',')
        coverage = whole + (f'<span class="decimal">,{decimal}</span>' if decimal else '') + '<span class="pct">%</span>'
        bar = render.coverage_bar(render.coverage_segments(money), total_percent=pct, owner_sheet=True)
    audit = status
    if facts.available:
        audit += '<dl>' + ''.join(f'<dt>{label}</dt><dd>{rub(value)}</dd>' for label, value in (
            ('Разница: расходы минус поступления', facts.uncovered_kopecks), ('Недостаёт', facts.shortfall_kopecks),
            ('Сверх расходов', facts.excess_kopecks), ('Результат до субсидии', facts.result_before_subsidy_kopecks),
            ('Результат после субсидии', facts.result_after_subsidy_kopecks))) + '</dl>'
        audit += render.hints.allocation_block(money.allocation)
    audit += '<h2>Исторические публикации</h2>' + render.historical_block(historical, period_label=period.label)
    brand = (f'<img class="brand-real-logo" src="{e(logo_url)}" alt="РанДом Культуры">' if logo_url else
             '<span class="brand-star" aria-hidden="true">✳</span><span class="brand-paper paper sand">РанДом<br>Культуры</span>')
    photo = (f'<figure class="photo-fragment"><div class="photo-underlay"></div><img src="{e(photo_url)}" '
             'alt="Встреча в подвале РанДома"></figure>'
             if photo_url else '')
    label = e(period.label)
    as_of = f'Снимок: {money.as_of:%d.%m.%Y}' if money else 'Нет снимка'
    preliminary_points = any(facts.partial or facts.coverage_is_final is not True
                             for _, facts in months if facts.coverage_percent is not None)
    trend_note = 'График не показывает прибыль. Он показывает, какая часть расходов компенсирована.'
    if not months:
        trend_note = 'Исторические периоды показываются только по сохранённым данным.'
    elif preliminary_points:
        trend_note += ' Предварительные месяцы отмечены точками без соединяющей линии.'
    return TEMPLATE.substitute(period=label, options=render.period_options(periods, period.label), brand=brand, photo=photo,
        as_of=as_of, coverage=coverage, bar=bar, stamp=stamp, footnote=footnote, trend_note=trend_note,
        caution=f'<p class="financial-caution">{caution}</p>' if caution else '',
        income=rub_compact(facts.compensation_kopecks), expense=rub_compact(facts.expense_kopecks),
        subsidy=rub_compact(facts.subsidy_kopecks),
        subsidy_note=('Внесли на содержание в этом месяце' if facts.subsidy_kopecks is not None
            else 'Фактические взносы нельзя приравнивать к недостающей сумме'),
        incoming=category_rows(facts.incoming, facts.incoming_sum_kopecks, facts.unclassified_in_kopecks, partial),
        outgoing=category_rows(facts.outgoing, facts.outgoing_sum_kopecks, facts.unclassified_out_kopecks, partial),
        trend=trend(months), audit=audit, csv=(f'<a id="csv-link" href="/api/public/export/categories.csv?period={label}" '
        'download>Скачать суммы по категориям ↗</a>' if money and period.kind in ('month', 'year') else 'CSV за период недоступен'))
