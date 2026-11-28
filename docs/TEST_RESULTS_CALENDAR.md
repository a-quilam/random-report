# Приёмка: собрания, закрытые аренды, исключённые подкалендари

Задача `t_2cf0c4d8`: отличить состоявшиеся собрания от прочих закрытых событий и вывести
в публичном календаре только день собрания. Правило и конфигурация — `docs/CALENDAR.md`,
изменённый контракт домена — `docs/DOMAIN.md` §§7–8, 11.8.

Все проверки — только код и синтетические данные. Секреты, приватные файлы, реальные
API, `push` и деплой не использовались; работа велась на локальной БД и каталоге во
временной папке теста.

## 1. Команды и результаты

```
cd /home/agent/projects/random-report
python3 -m unittest tests.test_calendar_meetings tests.test_meeting_word -v
python3 -W always::ResourceWarning -m unittest discover -s tests -t . -q
python3 -m randreport.web check
```

Реальные прогоны:

```
Ran 26 tests in 0.085s
OK
resource guard: no unclosed sqlite connection, listening socket or http response

Ran 389 tests in 11.278s
OK
resource guard: no unclosed sqlite connection, listening socket or http response

{"pages_checked": 120, "problems": [], "problem_count": 0}
```

Suite также зелёный на `python3.14` (`Ran 389 tests ... OK`, 0 ResourceWarning).

## 2. Что доказывается (карта тестов)

`tests/test_calendar_meetings.py` (21 тест) и `tests/test_meeting_word.py` (5 тестов):

| Проверка | Тест |
|---|---|
| Точное «Собрания» в разрешённом контуре → `meeting`, один подсчёт | `TestMeetingClassification.test_exact_title_in_the_contour_is_a_meeting_and_is_counted_once` |
| **Подстрока/длинное название — не собрание** (консервативно) и подсказка админу | `test_a_longer_title_in_the_contour_is_not_a_meeting`, `tests/test_meeting_word.py::test_word_inside_a_longer_title_is_not_a_meeting` |
| Точное название вне контура остаётся своим типом | `test_the_exact_title_outside_the_contour_stays_its_own_type` |
| Пересечение «контур собраний ∩ аренда» → агрегат (конфликт консервативно) | `test_a_record_in_both_the_meeting_contour_and_rent_is_resolved_conservatively` |
| Исключённый подкалендарь важнее контура собраний | `test_an_excluded_subcalendar_beats_the_meeting_contour` |
| Раздельная категория аренды; не каждое закрытое — аренда | `TestClosedRentSplitAndExclusion.test_not_every_closed_record_is_a_rent` |
| Исключённое не даёт ни списков, ни итогов | `test_excluded_gives_neither_lists_nor_totals` |
| Собрание публикуется как дата + константа | `TestPublicProjection.test_a_meeting_is_published_as_day_plus_constant_only` |
| **Никакой утечки** названия/id/времени/подкалендаря/URL/людей в JSON | `test_no_source_detail_of_a_meeting_reaches_the_payload` |
| Исходное название заменено константой (различимое название) | `test_the_payload_keeps_only_the_constant_not_the_source_title` |
| Закрытая аренда остаётся агрегатом и не попадает в сетку | `test_closed_rent_stays_aggregate_only_and_the_grid_hides_it` |
| Round-trip публичного DTO; у собрания нет лишних полей | `test_events_dto_round_trips_through_the_public_contract`, `test_a_meeting_dto_has_no_other_field` |
| Рендер сетки отмечает день собрания, деталей нет | `test_the_rendered_calendar_marks_the_meeting_day` |
| Конфигурация из окружения, дефолты ничего не продвигают | `TestConfiguration` (4 теста) |
| Стол записывает роли контура и показывает их в настройках | `TestDeskConfiguration` |
| Синтетический end-to-end Teamup → отчёт/календарь/JSON/CSV | `TestEndToEndTeamup.test_teamup_to_report_calendar_json_and_csv` |
| Явное удаление убирает собрание из следующего релиза (ревизия сохраняется) | `TestEndToEndTeamup.test_an_explicit_deletion_removes_the_meeting_from_the_next_build` |

## 3. Ключевые измеренные утверждения

* Публичный JSON собрания: `meetings == [{"on": "2025-07-19", "label": "Собрание"}]`,
  `meeting_days == {"2025-07-19": 1}`; в тексте нет `Собрания`, «Общее собрание
  коммуны», `occ_*`, `sub_*`, `19:00`, `cal_*`; `assert_no_leaks` проходит.
* `event_type_aggregates` в синтетике: `meeting 1`, `closed_rent 1`, `closed 2`,
  `internal_meeting 1`, `open 1`; исключённая запись не входит ни в `total` (6), ни в
  агрегаты.
* `closed_aggregate = closed + closed_rent = 3`; собрания считаются отдельно (не
  удваиваются).
* Дневная сетка: отмечен только день публичного события (`2025-07-03`) и день собрания
  (`2025-07-19`); дни закрытой аренды (21) и исключённой записи (23) отсутствуют.
* CSV (`export_categories_csv` за период) не содержит ни названия, ни даты собрания.

## 4. Приведённое к решению расхождение

Коммит `b3c25da` («Recognize meeting words within longer Teamup titles») добавил
`randreport/domain/meeting_title.py` с поиском слова «собрани…» внутри любого названия.
Это противоречило задаче («точное совпадение vs подстрока») и было **менее**
консервативным: любое длинное название с этим словом продвигало закрытую запись на
публичный календарь. Правило приведено к **точному** совпадению; сопоставление по
слову сохранено только как подсказка админу (`looks_like_meeting_title`,
`EventSelection.admin_flags`). Возможный конфликт отмечен в треде задачи.

## 5. Локальный коммит

Изменения только в локальном репозитории; `push`/деплой не выполнялись.
