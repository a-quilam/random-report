"""Точная база и реальный интервал в мелкой строке главной (вариант А, t_3da0472f).

До варианта А строка отвечала только на базу ровно за 30 суток. По решению
владельца от 28.11.2026 история ≥8 суток отвечает «+N за M дней» с фактическим
интервалом: база 31 день без точки ровно на 30-е сутки — крайняя доступная
точка, в подсказке реальные даты обеих крайних точек; двух замеров ближе
8 суток по-прежнему не хватает — прежнее честное состояние.
"""

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
    def test_one_day_older_states_the_real_interval(self):
        html=self.text(23)
        self.assertIn('+5 за 31 день',html)
        self.assertIn('2026-10-23',html)
    def test_two_close_measurements_stay_honest(self):
        # two measurements only 7 days apart: fewer than 8 days of history
        end=Point(on=date(2026,11,23),subscribers=105)
        start=Point(on=date(2026,11,16),subscribers=100)
        html=page(period_label='2026-11',money=None,events=None,historical={},community=Point(metrics={'latest_subscribers':105,'latest_on':'2026-11-23'},channel_points=(end,)),subscribers_extra=(start,))
        self.assertIn('Нет сравнения за 30 дней',html)
        self.assertNotIn('+5',html)
