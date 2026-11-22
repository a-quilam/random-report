"""Selected aggregate facts transcribed from public reports, not a live ledger."""
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from datetime import date
from functools import lru_cache

@dataclass(frozen=True)
class Report:
    start: str
    end: str
    published: str
    post: int
    events: int
    receipts: int | None = None  # Historical report units: whole RUB
    expenses: int | None = None


def archive():
    return (Report('2025-01','2025-01','2025-02-01',587,40),
            Report('2025-02','2025-02','2025-03-02',636,52),
            Report('2025-03','2025-03','2025-04-05',698,59),
            Report('2025-04','2025-08','2025-09-16',910,147),
            Report('2025-01','2025-12','2026-01-08',1091,459,707920,1267226))


def _index(month):
    d = date.fromisoformat(month+'-01')
    return 12*d.year+d.month


def lookup(start, end, metric):
    """Only sum disjoint complete periods; prefer the published exact period."""
    if metric not in ('events','receipts','expenses'): return None
    first,last = _index(start),_index(end)
    if first>last: raise ValueError('reversed period')
    rows = sorted(((_index(r.start),_index(r.end),getattr(r,metric)) for r in archive()),key=lambda r:-r[1])
    @lru_cache(None)
    def tile(a):
        if a==last+1: return 0
        for left,right,value in rows:
            if left==a and right<=last and value is not None:
                suffix=tile(right+1)
                if suffix is not None: return value+suffix
        return None
    return tile(first)


def render(r):
    fmt=lambda n: f'{n:,}'.replace(',',' ')
    body=f'<h1>Отчёт · {escape(r.start)} — {escape(r.end)}</h1><p>Состоялось мероприятий: <strong>{r.events}</strong>.</p>'
    if r.expenses is not None and r.receipts is not None:
        pct=Decimal(r.receipts)*100/Decimal(r.expenses)
        body+=f'<dl><dt>Поступления</dt><dd>{fmt(r.receipts)} ₽</dd><dt>Расходы</dt><dd>{fmt(r.expenses)} ₽</dd><dt>Покрытие</dt><dd>{pct:.2f}%</dd><dt>Разница до вклада коммунаров</dt><dd>{fmt(r.receipts-r.expenses)} ₽</dd></dl><p>Это не остаток на счетах и не фактический взнос коммунаров.</p>'
    return body+f'<p>Исторический агрегат. <a href="https://t.me/random_culture/{r.post}">Публикация от {escape(r.published)}</a>.</p>'
