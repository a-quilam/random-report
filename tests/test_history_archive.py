import unittest
from pathlib import Path
from history_archive import archive, lookup, render

class HistoryTests(unittest.TestCase):
    def test_annual_not_added_to_monthly(self):
        self.assertEqual(lookup('2025-01','2025-12','events'),459)
    def test_group_not_distributed(self):
        self.assertEqual(lookup('2025-04','2025-08','events'),147)
        self.assertIsNone(lookup('2025-07','2025-08','events'))
    def test_no_partial_totals(self):
        self.assertIsNone(lookup('2025-02','2025-11','events'))
        self.assertEqual(lookup('2025-02','2025-03','events'),111)
    def test_no_fabricated_balance(self):
        self.assertIsNone(lookup('2025-01','2025-12','balance'))
    def test_reports_have_original_and_no_private_fields(self):
        for item in archive():
            html=render(item)
            self.assertIn('https://t.me/random_culture/',html)
            self.assertNotIn('account_id',html)
    def test_net_result_not_a_balance(self):
        self.assertIn('559',render(archive()[-1]))
        self.assertIn('не остаток',render(archive()[-1]))
