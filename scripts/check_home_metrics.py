"""Compare published monthly homepage figures against its public API (read only)."""
import argparse
from decimal import Decimal
import json
import re
from urllib.request import urlopen


def audit(base, period):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
        raise ValueError('expected YYYY-MM')
    def get(path):
        with urlopen(base.rstrip('/') + path + '?period=' + period, timeout=15) as r:
            return r.read().decode()
    html = get('/')
    def section(name):
        d = json.loads(get('/api/public/' + name))
        if d['period']['requested'] != period:
            raise ValueError('wrong API period: ' + name)
        return d['metrics']
    money = section('money')
    expected = dict(eventsCount=section('events')['total_events'], subCount=section('community')['latest_subscribers'], got=money['compensation_kopecks'], spent=money['expense_kopecks'], percent=money['coverage_percent'])
    for key, value in expected.items():
        raw = re.search(r'id="'+key+r'">([^<]+)<', html)[1].strip()
        if value is None:
            if raw in ('—','нет данных'): continue
            raise ValueError('unexpected numeric value for '+key)
        actual=Decimal(''.join(raw.split()).rstrip('₽%').replace(',','.'))
        if key in ('got','spent'): actual *= 100
        if actual != Decimal(str(value)): raise ValueError(f'{key}: HTML {actual}, API {value}')
    return expected

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('base');parser.add_argument('period');a=parser.parse_args(); print(json.dumps(audit(a.base,a.period),ensure_ascii=False))
