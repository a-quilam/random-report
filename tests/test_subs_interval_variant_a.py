"""Вариант А (карточка t_3da0472f): «+N за M дней» по фактическим ночным замерам.

Клаузы карточки, каждая с собственным тестом:

* база ровно 30 дней → «+N за 30 дней»;
* история 31 сутки с ежедневными точками → берётся точка 30 дней назад;
* история 31 сутки без точки на 30-е сутки → крайняя доступная (самая старая),
  в подписи (hint) — реальные даты обеих использованных точек;
* 8 суток истории → «+N за 8 дней»;
* 7 суток истории → прежнее честное состояние («Нет сравнения за 30 дней»);
* пропуски нескольких дней внутри окна 30 дней → крайние доступные точки окна,
  N считается по ним, реальные даты в подписи;
* два замера ближе 8 суток → прежнее честное состояние;
* склонение: 21 день / 22 дня / 11 дней (render.plural);
* интервал длиннее окна (старшая точка далеко за 30 суток) печатает свою
  настоящую длину — никогда не подписывается «за 30 дней».

Все тесты офлайн: только фикстуры-точки, никаких внешних вызовов.
"""

from datetime import date, timedelta
from types import SimpleNamespace as Point
from unittest import TestCase
import re

from randreport.web import render
from randreport.web.approved_home import page

PERIOD = "2026-11"
END = date(2026, 11, 23)


def daily(start: date, count: int, base: int = 4000, step: int = 5) -> list[Point]:
    return [Point(on=start + timedelta(days=i), subscribers=base + step * i) for i in range(count)]


class DeltaRule(TestCase):
    """render.subscriber_delta: правило выбора базы и порог 8 суток."""

    def delta(self, *specs: tuple[date, int]) -> tuple[int | None, str | None, int | None]:
        return render.subscriber_delta([Point(on=d, subscribers=v) for d, v in specs])

    def test_exact_thirty_days(self) -> None:
        delta, base_on, span = self.delta((date(2026, 10, 24), 100), (END, 105))
        self.assertEqual(5, delta)
        self.assertEqual("2026-10-24", base_on)
        self.assertEqual(30, span)

    def test_thirty_one_days_of_daily_history_uses_the_thirty_day_point(self) -> None:
        history = daily(date(2026, 10, 23), 32)  # 2026-10-23 … 2026-11-23
        delta, base_on, span = render.subscriber_delta(history)
        self.assertEqual("2026-10-24", base_on)
        self.assertEqual(30, span)
        self.assertEqual(30 * 5, delta)

    def test_thirty_one_days_without_the_exact_point_takes_the_oldest(self) -> None:
        delta, base_on, span = self.delta((date(2026, 10, 23), 100), (END, 105))
        self.assertEqual(5, delta)
        self.assertEqual("2026-10-23", base_on)
        self.assertEqual(31, span)

    def test_eight_days_of_history_answer(self) -> None:
        delta, base_on, span = self.delta((date(2026, 11, 15), 100), (END, 103))
        self.assertEqual(3, delta)
        self.assertEqual("2026-11-15", base_on)
        self.assertEqual(8, span)

    def test_seven_days_of_history_is_still_no_history(self) -> None:
        self.assertEqual((None, None, None), self.delta((date(2026, 11, 16), 100), (END, 103)))

    def test_two_close_measurements_are_no_history(self) -> None:
        self.assertEqual((None, None, None), self.delta((date(2026, 11, 20), 100), (END, 105)))

    def test_gap_moves_the_base_to_the_earliest_point_still_inside_the_window(self) -> None:
        # Ежедневные точки 14.10–23.10, пропуск 24.10–08.11, затем ежедневно
        # 09.11–23.11. Окно 30 суток открывается 24.10, внутри окна первая
        # доступная точка — 09.11: крайние доступные точки окна 09.11 и 23.11.
        history = daily(date(2026, 10, 14), 10) + daily(date(2026, 11, 9), 15, base=4500)
        delta, base_on, span = render.subscriber_delta(history)
        self.assertEqual("2026-11-09", base_on)
        self.assertEqual(14, span)
        # N считается по датам использованных точек: база 09.11 (4500), свежий 23.11
        self.assertEqual(daily(date(2026, 11, 9), 15, base=4500)[-1].subscribers - 4500, delta)

    def test_a_base_far_outside_the_window_states_its_real_length(self) -> None:
        delta, base_on, span = self.delta((date(2026, 7, 1), 100), (END, 105))
        self.assertEqual(5, delta)
        self.assertEqual("2026-07-01", base_on)
        self.assertEqual(145, span)


class HomePageSubline(TestCase):
    """Мелкая строка главной: реальный интервал, честная пустота, даты в подписи."""

    def text(self, *specs: tuple[date, int]) -> str:
        points = [Point(on=d, subscribers=v) for d, v in specs]
        end = points[-1]
        return page(
            period_label=PERIOD,
            money=None,
            events=None,
            historical={},
            community=Point(
                metrics={"latest_subscribers": end.subscribers, "latest_on": end.on.isoformat()},
                channel_points=(end,),
            ),
            subscribers_extra=tuple(points[:-1]),
        )

    def test_exact_thirty_day_base_reads_plus_n_for_30_days(self) -> None:
        html = self.text((date(2026, 10, 24), 100), (END, 105))
        self.assertIn("+5 за 30 дней", html)
        self.assertIn("база — снимок на 2026-10-24", html)

    def test_thirty_one_day_base_without_exact_point_reads_the_real_interval(self) -> None:
        html = self.text((date(2026, 10, 23), 100), (END, 105))
        subline = re.search(r'id="subGrowth"[^>]*>(.*?)</span>', html).group(1)
        self.assertEqual("↗ +5 за 31 день", subline)
        self.assertIn("data-help", html)
        self.assertIn("2026-10-23", html)
        self.assertIn("2026-11-23", html)

    def test_eight_day_history_reads_plus_n_for_8_days(self) -> None:
        html = self.text((date(2026, 11, 15), 100), (END, 103))
        self.assertIn("+3 за 8 дней", html)
        self.assertIn("2026-11-15", html)

    def test_short_history_keeps_the_honest_line(self) -> None:
        html = self.text((date(2026, 11, 16), 100), (END, 103))
        self.assertIn("Нет сравнения за 30 дней", html)

    def test_gap_prints_the_extreme_available_points_and_their_dates(self) -> None:
        html = self.text((date(2026, 11, 9), 4495), (END, 4565))
        self.assertIn("+70 за 14 дней", html)
        self.assertIn("2026-11-09", html)
        self.assertIn("2026-11-23", html)


class PluralRule(TestCase):
    """Склонение интервала: 1 день / 2 дня / 5 дней / 12 дней / 21 / 22."""

    def line(self, span: int) -> str:
        return render._delta_line(5, "2026-10-01", span_days=span)

    def test_spans_decline(self) -> None:
        self.assertIn("за 1 день", self.line(1))
        self.assertIn("за 2 дня", self.line(2))
        self.assertIn("за 5 дней", self.line(5))
        self.assertIn("за 12 дней", self.line(12))
        self.assertIn("за 21 день", self.line(21))
        self.assertIn("за 22 дня", self.line(22))
        self.assertIn("за 11 дней", self.line(11))

    def test_long_interval_is_never_labelled_thirty_days(self) -> None:
        line = self.line(120)
        self.assertIn("за 120 дней", line)
        self.assertNotIn("за 30 дней", line)
