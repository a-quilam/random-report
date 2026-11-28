# Единая запись процентов: регрессионные тесты

Карточка `t_31a3ef88`, спецификация — `docs/PERCENT_FORMAT.md` §6. Базовая ревизия —
`main` `9a3e082` (после правки формата `f387674` в `t_afaaf773`). Изменён **только
файл тестов** (`tests/test_web_leaflet_composition.py`); код приложения, релизные
данные и приватный store не тронуты, выпуск не требуется.

## 1. Что добавлено

| Проверка (§6) | Тест |
| --- | --- |
| п.1–2: точки в процентах нет, запятая есть, форма ровно одна | `TestOnePercentFormAcrossTheLeaflet.test_no_surface_prints_a_dot_as_the_decimal_separator_of_a_percentage`, `…test_every_printed_percentage_uses_one_digit_and_a_plain_space`, `…test_the_surfaces_that_print_shares_print_the_comma_form` |
| п.3: в пределах одной таблицы доли и суммы — один разделитель | `…test_no_table_mixes_a_share_and_a_sum_in_two_separators` (+ `…test_a_table_does_print_a_share_next_to_a_sum` — иначе проверке не на что смотреть) |
| легенда графика, `aria`-подпись, текстовая таблица графика | `…test_the_coverage_chart_legend_and_aria_label_use_the_one_form`, `TestTheCoverageChartStringForm` (две проверки, дословные строки) |
| п.4: строка «требуют классификации» с фиксированными значениями | `TestTheUnclassifiedRow` (две проверки) |
| п.5: подмена старого формата обязана ронять охранные проверки | `TestTheGuardCatchesTheOldFormat.test_a_return_to_the_dot_is_caught_by_every_guard` |

Три охранные проверки вынесены в общий миксин `PercentFormAssertions`
(`assert_one_percent_form`, `assert_one_separator_per_table`,
`assert_documented_unclassified_rows`), поэтому тест подмены доказывает падение
**тех же** проверок, которые работают на листке, а не более слабых копий.

Поверхности проверки: `/`, `/money`, `/support`, `/events`, `/reports`,
`/reports/2025`, `/reports/2025-01` — страницы берутся из синтетического релиза
(`tests/dev_release.py`) через настоящий WSGI-вызываемый объект, то есть проверяется
тот же HTML, что получает браузер.

Объём: модуль `tests/test_web_leaflet_composition.py` — 26 → **37** тестов; полный
набор — 700 → **711**.

## 2. Прогоны (все — офлайн, без живого адреса)

```bash
python3     -W error::ResourceWarning -m unittest discover -s tests -t .   # 711 OK
python3.14  -W error::ResourceWarning -m unittest discover -s tests -t .   # 711 OK
python3 -m compileall -q randreport tests                                  # чисто
python3 -m unittest tests.test_web_leaflet_composition                     # 37 OK
```

## 3. Красное до правки и при подмене формата

Оба прогона выполнены в одноразовых `git worktree` (основное дерево не менялось),
модуль тестов скопирован в них без изменений.

**а) Дерево до правки формата** (`8256fed`, до `f387674`):

```
Ran 37 tests
FAILED (failures=20, errors=1)
```

Падают пункты 1–4: страницы печатают `0.4` рядом с `340,59 ₽`, строка «требуют
классификации» — доля с точкой, `aria`-подпись и легенда — тоже. Ошибка — тест
подмены: на том дереве нет единственной точки форматирования (`render.format_percent`),
подменять нечего.

**б) Искусственный возврат старого формата на `main`** (`git revert --no-commit f387674`
в рабочем дереве, тесты — новые):

```
Ran 37 tests
FAILED (failures=20, errors=1)
```

Среди падений — `test_no_surface_prints_a_dot_as_the_decimal_separator_of_a_percentage`
(`/`, `/money`, `/events`, `/reports/2025`), `test_no_table_mixes_a_share_and_a_sum_in_two_separators`
(`/money`, `/`), обе части `test_the_coverage_chart_legend_and_aria_label_use_the_one_form`,
`test_the_legend_the_aria_label_and_the_text_table_share_the_form`,
`test_an_unknown_share_keeps_the_chart_word` и оба теста `TestTheUnclassifiedRow`.
Критерий «при возврате старого формата хотя бы один тест падает» выполнен с запасом:
падает 20, причём именно те, что защищают таблицы, легенду, `aria`-подпись и строку
«требуют классификации».

**в) Внутри набора** тест `TestTheGuardCatchesTheOldFormat` подменяет единственный
форматтер (`render.format_percent`) на прежний — возвращающий точку — и требует
`AssertionError` от всех трёх охранных проверок. Так проверка на подмену выполняется
при каждом обычном прогоне, а не только руками.

## 4. Строка «требуют классификации»: зафиксированные значения

`TestTheUnclassifiedRow` собирает период с теми же числами, что `docs/TEST_RESULTS_RELEASE.md`
§5 и `docs/PERCENT_FORMAT.md` §8 цитируют с живого адреса, и требует **дословного**
совпадения обеих строк:

```html
<tr><th scope="row"><span class="cat-name">требуют классификации</span><span class="cat-kind">категория требует проверки</span><svg class="minibar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="0" width="3.2" height="4"></rect></svg></th><td class="num">2,28 ₽</td><td class="num">3,2 %</td><td class="num">—</td></tr>
<tr><th scope="row"><span class="cat-name">требуют классификации</span><span class="cat-kind">категория требует проверки</span><svg class="minibar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="0" width="96.8" height="4"></rect></svg></th><td class="num">68,80 ₽</td><td class="num">96,8 %</td><td class="num">—</td></tr>
```

Обе строки сверены с боевым релизом напрямую (сырой сокет `127.0.0.1:8099`,
`/money?period=2026-11`, 2026-11-23): совпадают побайтово, то есть фиксируются
фактические значения, а не пересказ документа. Тот же тест отдельно требует, чтобы
доля печаталась с запятой, а **геометрия** `minibar` осталась числовой
(`width="3.2"`, `width="96.8"`, `width="3,2"` — отсутствует): правило §2.4 не
затронуто.

## 5. Границы проверки

* Тесты офлайн и не ходят на боевой адрес: живой релиз проверяет приёмочная карточка
  `t_908c2675` (§5 `docs/PERCENT_FORMAT.md`).
* Тест подмены опирается на существование единственного форматтера; на дереве до
  `f387674` он падает не утверждением, а `AttributeError` — «красный до правки» верно,
  но по другой причине (см. §3а).
* Проверяется синтетический релиз `2025-07`; числа строки «требуют классификации»
  фиксируются DTO, поэтому от фикстуры они не зависят.
