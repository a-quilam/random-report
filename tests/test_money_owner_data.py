"""Money page facts come independently from an allowlisted public snapshot."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest import TestCase

from randreport.domain.dto import CategoryDTO, MoneyDTO, PeriodDTO
from randreport.web.money_owner_data import money_owner_data


DAY = date(2025, 4, 8)


def snapshot(**metrics):
    return MoneyDTO(PeriodDTO('2025-03', None, None, True), DAY, 'public',
                    'fresh', 'operational', 'complete', (), 'kopecks', metrics, ())


class MoneyOwnerTests(TestCase):
    def test_independent_nullable_amounts(self):
        fields = ('expense_kopecks', 'compensation_kopecks', 'uncovered_kopecks',
                  'shortfall_kopecks', 'excess_kopecks', 'result_before_subsidy_kopecks',
                  'result_after_subsidy_kopecks', 'subsidy_kopecks', 'balance_kopecks',
                  'unclassified_in_kopecks', 'unclassified_out_kopecks')
        for field in fields:
            for value in (0, 431):
                with self.subTest(field=field, value=value):
                    page = money_owner_data(snapshot(**{field: value}))
                    for name in fields:
                        self.assertEqual(getattr(page, name), value if name == field else None)
                    self.assertEqual(page.balance_as_of, DAY if field == 'balance_kopecks' else None)
        absent = money_owner_data(None)
        self.assertFalse(absent.available)
        self.assertTrue(all(getattr(absent, name) is None for name in fields))

    def test_published_coverage_needs_a_positive_denominator(self):
        for spent, got, rate, expected in ((0, 0, '0', None), (None, 1, '100', None),
                                          (100, None, '0', None), (100, 0, '0', Decimal(0)),
                                          (100, 130, '130.0', Decimal('130.0')),
                                          (100, 80, None, None)):
            with self.subTest(spent=spent, got=got, rate=rate):
                page = money_owner_data(snapshot(expense_kopecks=spent, compensation_kopecks=got,
                                                coverage_percent=rate))
                self.assertEqual(page.coverage_percent, expected)
                self.assertEqual(page.expense_kopecks, spent)

    def test_category_totals_count_each_public_row_once_in_its_direction(self):
        a = CategoryDTO('a', 'A', 'external_compensation', 'in', 107, '1.07', None, None, 'Approved')
        b = replace(a, key='b', label='B', amount_kopecks=3, hint=None)
        c = replace(a, direction='out', bucket='operating_expense', amount_kopecks=290)
        subsidy = replace(b, bucket='commune_subsidy', amount_kopecks=999)
        dto = replace(snapshot(expense_kopecks=278, compensation_kopecks=89, subsidy_kopecks=917,
                               transactions='private-sentinel'), categories=(a, b, a, subsidy, c))
        page = money_owner_data(dto)
        self.assertEqual([(r.label, r.amount_kopecks, r.hint) for r in page.incoming],
                         [('A', 107, 'Approved'), ('B', 3, None)])
        self.assertEqual(page.incoming_sum_kopecks, 110)
        self.assertEqual(page.outgoing_sum_kopecks, 290)
        self.assertEqual(page.expense_kopecks, 278)
        self.assertEqual(page.compensation_kopecks, 89)
        self.assertEqual(page.subsidy_kopecks, 917)
        self.assertNotIn('private-sentinel', repr(page))
        self.assertIsNone(money_owner_data(None).incoming_sum_kopecks)
        self.assertEqual(money_owner_data(snapshot(category_sum_kopecks=0)).outgoing_sum_kopecks, 0)

    def test_partial_reasons_are_public_codes_not_free_text(self):
        dto = replace(snapshot(expense_kopecks=500, compensation_kopecks=0, coverage_percent='0',
                               coverage_is_final=False, unclassified_in_kopecks=80,
                               status_labels='quarantine_present,private-sentinel'), completeness='partial')
        page = money_owner_data(dto)
        self.assertTrue(page.partial)
        self.assertFalse(page.coverage_is_final)
        self.assertIn('quarantine_present', page.partial_reasons)
        self.assertIn('unclassified_present', page.partial_reasons)
        self.assertNotIn('private-sentinel', repr(page))
        self.assertEqual(page.compensation_kopecks, 0)
        self.assertEqual(page.coverage_percent, Decimal(0))
        self.assertIsNone(page.outgoing_sum_kopecks)
        zero = money_owner_data(snapshot(expense_kopecks=0, coverage_is_final=False))
        self.assertFalse(zero.partial)  # undefined ratio does not invalidate a confirmed zero
        self.assertEqual(zero.partial_reasons, ())

    def test_malformed_metric_does_not_discard_other_facts(self):
        for raw in ('not a percent', 'NaN', 'Infinity'):
            with self.subTest(raw=raw):
                page = money_owner_data(snapshot(expense_kopecks=10, compensation_kopecks=4,
                                                coverage_percent=raw, subsidy_kopecks=False))
                self.assertIsNone(page.coverage_percent)
                self.assertIsNone(page.subsidy_kopecks)
                self.assertEqual(page.compensation_kopecks, 4)
