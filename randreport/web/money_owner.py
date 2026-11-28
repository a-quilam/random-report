"""Owner's standalone SSR document. Only allowlisted snapshot facts enter the paper."""
from pathlib import Path
from string import Template

from . import render
from .money_owner_data import money_owner_data
from .render import e, rub_from_kopecks as rub

TEMPLATE = Template(Path(__file__).with_name('money_owner.html').read_text(encoding='utf-8'))


def category_rows(rows, total, unclassified, partial):
    parts = []
    for row in rows:
        hint = render.hints.hint_disclosure(row.hint, label=row.label)
        label = f'<span class="cat-name">{e(row.label)}</span>{hint}'
        parts.append(f'<div class="cat-row"><div>{label}</div><span class="cat-value">{rub(row.amount_kopecks)}</span></div>')
    if not parts and total is None:
        parts.append('<p class="cat-note">Сведений по категориям нет.</p>')
    for label, value in [('Разнесено по категориям', total), ('Ещё не разобрано', unclassified)]:
        if partial or value is not None:
            parts.append(f'<div class="cat-row">{label}<span class="cat-value">{rub(value)}</span></div>')
    return ''.join(parts)


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
        shapes.append(f'<text x="{x}" y="182" text-anchor="middle" font-size="16">{label[5:]}.{label[2:4]}</text>')
        shapes.append(f'<text x="{x}" y="{y-10}" text-anchor="middle" font-size="15">{render.percent_text(value)}</text>')
        shapes.append(f'<circle cx="{x}" cy="{y}" r="7" fill="#B22624"/>')
        prev = idx, x, y, preliminary
        suffix = ' · предварительно' if preliminary else ''
        rows.append(f'<li><a href="/money?period={label}">{label}</a>: {render.percent_text(value)}{suffix}</li>')
    return ('<svg viewBox="-12 -10 820 205" aria-hidden="true"><path d="M20 162H768" stroke="#B8AB9F"/>'
            + ''.join(shapes) + '</svg><details><summary>текстовая версия графика</summary><ul>' + ''.join(rows) + '</ul></details>')


def page(*, period, periods, money, months, status, historical, photo_url, logo_url):
    facts = money_owner_data(money)
    pct = facts.coverage_percent
    partial = facts.partial or (pct is not None and facts.coverage_is_final is not True)
    stamp, caution = 'ПЕРИОД РАЗНЕСЁН', ''
    if not facts.available:
        stamp, caution = 'ПОКА НЕТ ДАННЫХ', 'Текущих финансовых данных за этот период нет.'
    elif partial:
        stamp, caution = 'ПРЕДВАРИТЕЛЬНО', 'Снимок неполон: известные суммы и покрытие — не итог периода.'
    elif facts.expense_kopecks == 0:
        stamp, caution = 'РАСХОДОВ НЕ БЫЛО', 'При нулевых расходах процент не рассчитывается.'
    coverage, bar = '—', ''
    if pct is not None and not (pct == 0 and partial):
        formatted = render.percent_text(pct)
        whole, _, decimal = formatted.removesuffix(' %').partition(',')
        coverage = whole + (f'<span class="decimal">,{decimal}</span>' if decimal else '') + '<span class="pct"> %</span>'
        bar = render.coverage_bar(render.coverage_segments(money), total_percent=pct, owner_sheet=True)
    audit = status
    if facts.available:
        audit += '<dl>' + ''.join(f'<dt>{label}</dt><dd>{rub(value)}</dd>' for label, value in (
            ('Разница: расходы минус поступления', facts.uncovered_kopecks), ('Недостаёт', facts.shortfall_kopecks),
            ('Сверх расходов', facts.excess_kopecks), ('Результат до субсидии', facts.result_before_subsidy_kopecks),
            ('Результат после субсидии', facts.result_after_subsidy_kopecks))) + '</dl>'
        audit += render.hints.allocation_block(money.allocation)
    audit += '<h2>Исторические публикации</h2>' + render.historical_block(historical, period_label=period.label)
    brand = (f'<img class="brand-star" src="{e(logo_url)}" alt="Логотип РанДома">' if logo_url else
             '<span class="brand-star" aria-hidden="true">✳</span>')
    photo = (f'<figure class="photo-fragment"><div class="photo-underlay"></div><img src="{e(photo_url)}" '
             'alt="Встреча в подвале РанДома"><figcaption>Наш подвал. Его содержание — не абстракция.</figcaption></figure>'
             if photo_url else '')
    label = e(period.label)
    return TEMPLATE.substitute(period=label, options=render.period_options(periods, period.label), brand=brand, photo=photo,
        as_of=f'Снимок: {money.as_of}' if money else 'Нет снимка', coverage=coverage, bar=bar, stamp=stamp,
        caution=f'<p class="financial-caution">{caution}</p>' if caution else '',
        income=rub(facts.compensation_kopecks), expense=rub(facts.expense_kopecks), subsidy=rub(facts.subsidy_kopecks),
        balance=rub(facts.balance_kopecks), subsidy_note=('Фактически внесли на содержание' if facts.subsidy_kopecks is not None
            else 'Фактические взносы нельзя приравнивать к недостающей сумме'),
        incoming=category_rows(facts.incoming, facts.incoming_sum_kopecks, facts.unclassified_in_kopecks, partial),
        outgoing=category_rows(facts.outgoing, facts.outgoing_sum_kopecks, facts.unclassified_out_kopecks, partial),
        trend=trend(months), trend_note=('Предварительные наблюдения показаны точками. Линия соединяет только соседние месяцы с итоговыми значениями.'
            if months else 'История появится после публикации месячных снимков.'), audit=audit, csv=(f'<a id="csv-link" href="/api/public/export/categories.csv?period={label}" '
        'download>Скачать суммы по категориям ↗</a>' if money and period.kind in ('month', 'year') else 'CSV за период недоступен'))
