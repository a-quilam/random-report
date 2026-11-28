from datetime import date
from types import SimpleNamespace as Point
from unittest import TestCase
from randreport.web.approved_home import page

class ExactWindow(TestCase):
    def text(self, day):
        end=Point(on=date(2026,11,23),subscribers=105)
        start=Point(on=date(2026,10,day),subscribers=100)
        return page(period_label='2026-11',money=None,events=None,historical={},community=Point(metrics={'latest_subscribers':105,'latest_on':'2026-11-23'},channel_points=(end,)),subscribers_extra=(start,))
    def test_exact_base(self):self.assertIn('+5 за 30 дней',self.text(24))
    def test_older_not_substitute(self):self.assertIn('Нет сравнения за 30 дней',self.text(23))
