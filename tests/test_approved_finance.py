"""Public home finance fragment: two denominators, one short audited ledger."""
from datetime import date
from types import SimpleNamespace
import unittest
from xml.etree import ElementTree as ET

from randreport.domain.dto import CategoryDTO, MoneyDTO, PeriodDTO
from randreport.web import render
from randreport.web.home_finance_approved import finance


def category(label, amount, bucket='external_compensation', direction='in'):
    return CategoryDTO('PRIVATE_KEY', label, bucket, direction, amount, '', '99', None)


def snapshot(received, spent, rate, categories=(), final=True):
    day = date(2025, 2, 28)
    return MoneyDTO(PeriodDTO('2025-02', day, day, True), day, 'PRIVATE_RELEASE',
                    'fresh', 'verified', 'complete', (), 'RUB',
                    {'compensation_kopecks': received, 'expense_kopecks': spent,
                     'coverage_percent': rate, 'coverage_is_final': final}, tuple(categories))


def fragment(money, historical=None):
    return ET.fromstring(finance(money, '<period>', historical or {}))


def widths(root):
    return [int(c[1:]) for node in root.findall('./div[@class="bar"]/span')
            for c in node.attrib['class'].split() if c.startswith('q')]


class ApprovedFinance(unittest.TestCase):
    def test_approved_structure_and_public_binding(self):
        rows = [category('A<&', 4000), category('B', 2000), category('C', 1200),
                category('D', 800), category('PRIVATE_SUBSIDY', 50000, 'commune_subsidy'),
                category('PRIVATE_OUT', 30000, direction='out')]
        root = fragment(snapshot(8000, 20000, '40', rows))
        self.assertEqual([e.get('class') for e in root],
                         ['finance-heading', 'fin-top', 'bar', 'fin-legend', 'source-sheet', 'financial-note'])
        self.assertIn('<period>', root.findtext('.//*[@class="financial-period"]', ''))
        self.assertEqual(root.findtext('.//*[@class="fin-percent"]'), '40,0 %')
        sheet = root.findall('.//*[@class="source-row helpful"]')
        self.assertEqual([(r[0].text, r[1].text, r[2].text) for r in sheet],
                         [('A<&', '40,00 ₽', '50,0 %'), ('B', '20,00 ₽', '25,0 %'),
                          ('Прочее', '20,00 ₽', '25,0 %')])
        self.assertEqual(widths(root), [20, 10, 10, 60])
        self.assertNotIn('PRIVATE', ET.tostring(root, encoding='unicode'))
        self.assertFalse(any('style' in e.attrib or e.tag == 'script' for e in root.iter()))

    def test_source_row_hints_are_hover_plates_not_disclosures(self):
        from dataclasses import replace
        rows = [replace(category('Донат', 50), hint='От гостей <и друзей>'),
                category('Встречи', 30), category('Книги', 12), category('Склад', 8)]
        money = snapshot(100, 250, '40', rows, final=False)
        money.metrics['uncovered_kopecks'] = 150
        root = ET.fromstring(finance(money, '2026-11', {}))
        # the owner-approved mechanic: a data-help plate that floats on hover/focus
        ledger = root.findall('.//*[@id="sources"]/div[@class="source-row helpful"]')
        helps = [row.get('data-help') for row in ledger]
        self.assertEqual(len(helps), 3)
        self.assertIn(rows[0].hint, helps[0])
        self.assertIn('Встречи', helps[1])
        self.assertIn('Книги, Склад', helps[2])
        # a hint is never a click disclosure on the sheet, and stays keyboard-reachable
        self.assertFalse(root.findall('.//details'))
        self.assertTrue(all(row.get('tabindex') == '0' for row in ledger))
        self.assertEqual(root.findtext('.//*[@class="financial-period"]'), 'ноябрь')
        self.assertEqual(root.findtext('.//*[@id="finNote"]'), 'НЕ ПОКРЫТО: 1,50 ₽')
        money.metrics['uncovered_kopecks'] = None
        self.assertIn('НЕ ПОКРЫТО: нет данных', finance(money, '2026-11', {}))

    def test_overcoverage_proportional_and_bounded(self):
        root = fragment(snapshot(300, 200, '150', [category(x, 100) for x in 'ABC']))
        self.assertEqual(widths(root), [34, 33, 33, 0])
        self.assertEqual(root.findtext('.//*[@class="fin-percent"]'), '150,0 %')
        self.assertEqual(root.findall('div[@class="bar"]')[0].get('aria-valuenow'), '100')

    def test_inconsistent_breakdown_uses_aggregate_only(self):
        root = fragment(snapshot(300, 500, '60', [category('Incomplete detail', 100)]))
        self.assertEqual(widths(root), [60, 40])
        self.assertNotIn('Incomplete detail', ''.join(root.itertext()))
        self.assertFalse(root.findall('.//*[@class="source-row helpful"]'))

    def test_null_published_rate_is_not_filled_from_archive(self):
        root = fragment(snapshot(300, 500, None),
                        {'receipts': SimpleNamespace(value=12), 'expenses': SimpleNamespace(value=48)})
        self.assertFalse(widths(root))
        self.assertEqual(root.findtext('.//*[@class="fin-percent"]'), '—')

    def test_partial_or_missing_is_not_confirmed_zero(self):
        for money in (None, snapshot(0, 100, '0', final=False), snapshot(None, 100, None)):
            root = fragment(money)
            self.assertFalse(widths(root))
            self.assertNotIn('0,00 ₽', ''.join(root.itertext()))
            self.assertEqual(root.findtext('.//*[@class="fin-percent"]'), '—')
        root = fragment(snapshot(300, 600, '50', final=False))
        self.assertEqual(root.findtext('.//*[@class="fin-percent"]'), '50,0 %')
        # The caption plate is an overlay, so it stays one short line: the owner
        # read a 340×200 plaque as a defect of the mechanic. The service facts
        # (snapshot date, «предварительно», the denominator rule) are long text and
        # do not belong in the plate — they stay in the reader's reach on the sheet
        # («О выпуске» names the completeness) and on the money page.
        caption = root.find('.//*[@class="fin-caption helpful"]')
        self.assertIsNotNone(caption)
        plate = caption.get('data-help') or ''
        self.assertLessEqual(len(plate), 90)
        self.assertNotIn('предваритель', plate.lower())
        self.assertNotIn('2025', plate)
        # the partial state is still disclosed, and never as a confirmed zero
        money = snapshot(300, 600, '50', final=False)
        self.assertEqual(render.money_state(money), render.PARTIAL)
        section = render.money_section(money, period_label='2025-02', historical={})
        self.assertIn('не разнесена по категориям', section)

    def test_confirmed_zero_receipts_vs_zero_expense(self):
        root = fragment(snapshot(0, 100, 0))
        self.assertEqual(root.findtext('.//*[@class="fin-percent"]'), '0,0 %')
        self.assertEqual(widths(root)[-1], 100)
        root = fragment(snapshot(100, 0, None))
        self.assertFalse(widths(root))
        self.assertIn('0,00 ₽', [node.text for node in root.findall('.//b')])

    def test_archive_rubles_and_no_invented_sources(self):
        history = {'receipts': SimpleNamespace(value=12), 'expenses': SimpleNamespace(value=48)}
        root = fragment(None, history)
        self.assertEqual(root.findtext('.//*[@class="fin-percent"]'), '25,0 %')
        self.assertIn('12 ₽', [n.text for n in root.findall('.//b')])
        self.assertIn('опубликован', ''.join(root.itertext()).lower())
        self.assertFalse(root.findall('.//*[@class="source-row helpful"]'))
        self.assertFalse(widths(fragment(None, {'receipts': history['receipts']})))
