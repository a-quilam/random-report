# Результаты и доказательства тестов

Задача: домен и тесты `F01–F07`, `E01–E03`, `R02` (docs/REQUIREMENTS.md §§6–7, 10, 13).
Порядок работы — тесты раньше кода; ниже приведены реальные запуски.

## Команды

```
cd /home/agent/projects/random-report
python3 -m unittest discover -s tests -t . -v      # весь suite
python3 -m compileall -q randreport tests          # синтаксическая проверка
```

## RED — тесты до кода (01:31, до появления `randreport/`)

```
ERROR: tests.test_e01_completed_events (unittest.loader._FailedTest.…)   [и так 8 модулей]
ModuleNotFoundError: No module named 'randreport'
Ran 14 tests in 0.008s
FAILED (errors=8)
```

## Первый GREEN-прогон — suite поймал 5 падений и 1 ошибку

```
ERROR: test_moscow_offset_is_not_the_host_timezone                    (тест: MSK.utcoffset(None))
FAIL: test_non_event_calendars_are_kept_in_totals_not_dropped (публичным считался preparation)
FAIL: test_hostile_parameters_are_rejected_outright (params={'period': '2025-07-01'})
FAIL: test_hostile_parameters_are_rejected_outright (params={'period': '2025-04-05..2025-04-09'})
FAIL: test_new_category_is_discovered_and_not_folded_into_compensation (unclassified_in = 0)
FAIL: test_ruble_strings_become_integer_kopecks (0.015 → 2 копейки по half-even)
Ran 91 tests in 0.054s
FAILED (failures=5, errors=1)
```

Исправлено в коде: `validate_params` теперь проверяет и значение `period`
(единый отказ до чтения данных); `unclassified` сохраняет направление самого
движения (был `unknown`, терялся `in`); публичными для списка и дневной сетки
считаются только `open`/`open_by_registration`. Исправлено в тестах (ошибки
ожиданий, не поведения): округление `0.015` по half-even → 0.02,
`ZoneInfo.utcoffset` требует aware-datetime.

## Итог

```
Ran 94 tests in 0.073s

OK
```

94 = 88 моих тестов + 6 тестов `tests/test_history_archive.py` (исторический
модуль `history_archive.py`, файл и модуль принадлежат соседней карточке
исторической реконструкции, уже в git; в этом наборе только прогоняются).

| Модуль | Тестов |
|---|---|
| tests/test_f01_f02_formulas.py | 11 |
| tests/test_f03_f04_classification.py | 15 |
| tests/test_f05_reconciliation.py | 9 |
| tests/test_f06_precision.py | 7 |
| tests/test_f07_balance.py | 10 |
| tests/test_finance_integrity_properties.py | 3 |
| tests/test_e01_completed_events.py | 8 |
| tests/test_e02_occurrences.py | 9 |
| tests/test_e03_r02_privacy.py | 16 |

Прочее: `compileall` — ok. CLI `pyright` в окружении не установлен (доступен
только LSP-сервер), поэтому статическая проверка типов ограничена
диагностикой редактора; проверка `python3 -c "import …"`/compileall пройдена.

## Соответствие матрице приёмки

| ID | Тесты |
|---|---|
| F01 | `test_formula_values_match_the_required_example`, `test_balance_is_not_derived_from_the_deficit`, `test_amounts_never_appear_as_float` |
| F02 | `TestF02EdgeStates` (110%+excess; E=0; E<0; отсутствующая субсидия; подтверждённый ноль; отсутствие данных; отрицательная чистая компенсация) |
| F03 | `test_contribution_and_compensation_are_separate`, `test_direct_payment_and_reimbursement_are_not_double_counted`, `test_mixed_living_public_category_stays_in_quarantine`, `test_quarantine_is_not_silently_added_to_unclassified`, `test_new_account_is_not_included_automatically` |
| F04 | `TestF04FlowKinds` (переводы внутри контура, кросс-валютный, счёт вне контура, комиссия, возврат покупки, отмена доната, заём/погашение, корректировка баланса, плановая, удалённая) |
| F05 | `TestF05NewCategories` (62819+72000+5750+22400=162969, новая категория, месяц=год, единый период апрель–август), `TestF05HistoricalConflict`, `TestF05WeightedAnnual` |
| F06 | `TestF06Precision` (копейки, 500 сумм, totals==суммы категорий, weighted-год, границы январь/декабрь, МСК) |
| F07 | `TestF07Balance` (ликвидные публичные счета, валюта без курса, обязательства, неизвестный исторический остаток, подтверждённый ноль, неподтверждённый снимок, дефицит ≠ баланс) |
| E01 | `TestE01Completion` (будущее/текущее, `end == cutoff`, ночное, многодневное, all-day exclusive end, московская граница, неизвестная длительность) |
| E02 | `TestE02Occurrences` (серии, несколько subcalendar, перенос, явное удаление, retention, unknown-тип, тип из mapping, конфликт приватности, внутренние/подготовка) |
| E03 | `TestE03ClosedEvents` (закрытые: ни названия, ни даты, ни id, ни subcalendar в JSON; день закрытого отсутствует в сетке; год — только месячные агрегаты) |
| R02 | `TestR02QueryAllowlist` (allowlist параметров, отказ на день/диапазон/`granularity=day`/`account_id`/`occurrence_id`, CSV только категории), `TestR02StrictDTO` (round-trip, unknown-поля, float, null-субсидия, скрытые счётчики, детектор утечки) |
| Финансовая целостность | `TestFinancialIntegrityInvariants` (200 комбинаций E/C/S: тождества, отсутствие float, сумма строк = итог) |

Тесты `TestFinancialIntegrityInvariants` добавлены после реализации как
инвариантные (характеризующие) проверки и один раз поймали ошибку в моём же
ожидании знака `D = E − C` — это отмечено выше как ошибка теста, не кода.
