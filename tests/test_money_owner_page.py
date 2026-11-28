"""Exercise the owner document through the public WSGI boundary."""
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from randreport.web.app import PublicSite
from tests.test_money_owner_data import snapshot
from tests.test_web_public_site import SiteTestCase, PERIOD, make_wsgi_request


class OwnerRouteTests(SiteTestCase):
    def test_selected_release_is_a_standalone_server_document(self):
        code, page, _ = self.fixture.get('/money', 'period=' + PERIOD)
        self.assertEqual(code, 200)
        self.assertIn('/static/money.css', page)
        for tag in ('main', 'header', 'footer'):
            self.assertEqual(page.count('<' + tag + ' '), 1)
        # the approved sheet repeats the section navigation at the bottom
        self.assertEqual(page.count('<nav '), 2)
        self.assertNotIn('<script', page)
        self.assertNotIn(' style=', page)
        self.assertIn('value="' + PERIOD + '" selected', page)
        self.assertIn('/api/public/export/categories.csv?period=' + PERIOD, page)
        self.assertIn('ПРЕДВАРИТЕЛЬНО', page)

    def test_the_owner_sheet_v1_2_composition(self):
        code, page, _ = self.fixture.get('/money', 'period=' + PERIOD)
        self.assertEqual(code, 200)
        # «Остаток в кассе» is not published on the money sheet
        self.assertNotIn('balance_kopecks', page)
        self.assertNotIn('ОСТАТОК В КАССЕ', page)
        # the purple wedge is explained by the short aside, never by a balance
        self.assertIn('А КОММУНАРЫ?', page)
        self.assertIn('ПРО ВЗНОСЫ', page)
        # the top navigation repeats the home order and colours; bottom too
        for slip in ('Летопись', 'Методология'):
            self.assertEqual(page.count('>' + slip + '</a>'), 2)
        # the owner removed the old filler caption under the photo
        self.assertNotIn('не абстракция', page)
        # one period ticket in the header, labelled «ПЕРИОД»
        self.assertEqual(page.count('period-plate'), 1)
        self.assertIn('<label for="period-select">ПЕРИОД</label>', page)

        # the owner's ledger: grey share note under each labelled row
        self.assertIn('<span class="cat-share">61,5% от суммы</span>', page)
        # a filled sheet shows only real categories; the two service rows
        # belong to the partial state or to a non-zero remainder
        complete = self.fixture.get('/money', 'period=' + PERIOD)[1]
        if 'ПРЕДВАРИТЕЛЬНО' not in complete:
            self.assertNotIn('Разнесено по категориям', complete)
            self.assertNotIn('Ещё не разобрано', complete)

    def test_missing_period_offers_neither_export_nor_fake_percentage(self):
        code, page, _ = self.fixture.get('/money', 'period=2040-01')
        self.assertEqual(code, 200)
        self.assertIn('ПОКА НЕТ ДАННЫХ', page)
        self.assertNotIn('id="csv-link"', page)
        self.assertNotIn('class="segments-bar"', page)


class PeriodSnapshots(unittest.TestCase):
    def test_only_final_adjacent_months_connect_and_verified_zero_is_not_missing(self):
        with TemporaryDirectory() as tmp:
            for label, rate in [('2025-01','50'), ('2025-02','60'), ('2025-04','40'),
                                ('2025-05',None), ('2025-06','90'), ('2025','99')]:
                dto = snapshot(expense_kopecks=100 if rate else 0, compensation_kopecks=0 if not rate else int(rate),
                               coverage_percent=rate, coverage_is_final=bool(rate), balance_kopecks=0)
                dto = replace(dto, period=replace(dto.period, requested=label))
                dest = Path(tmp) / ('rel_' + label)
                dest.mkdir()
                (dest/'index.json').write_text('{}')
                (dest/'money.json').write_text(json.dumps(dto.to_public_dict()))
            site = PublicSite(release_dir=tmp)
            status, page = make_wsgi_request(site, '/money', 'period=2025-05')
            self.assertEqual(status, 200)
            self.assertIn('РАСХОДОВ НЕ БЫЛО', page)
            # the balance never enters the owner sheet, the subsidy wedge does
            self.assertNotIn('balance_kopecks', page)
            self.assertIn('data-field="subsidy_kopecks">нет данных', page)
            graph = page.split('id="trend-graph"', 1)[1].split('class="trend-text"', 1)[0]
            self.assertEqual(graph.count('<line '), 1)
            self.assertEqual(graph.count('<circle '), 3)
            for label in ('2025-01', '2025-02', '2025-04'):
                self.assertIn(f'/money?period={label}"', graph)
            for label in ('2025-03', '2025-05', '2025-06', '2025'):
                self.assertNotIn(f'/money?period={label}"', graph)
