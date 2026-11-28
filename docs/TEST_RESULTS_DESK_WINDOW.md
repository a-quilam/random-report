# Доказательства: карточка операции и оба действия вне окна списка (t_48daeeb6)

Карточка — **дефект на уже собранном `main`**: стол ищет операцию только в выдаче
`money_rows(conn, limit=200)`, поэтому операции «частично относящихся» категорий
(`per_operation`) старше окна недостижимы. База — `0bfdfa7` (`main`), коммит
локальный: ни слияний, ни публикации. Всё на синтетике: реальные данные, секреты
и приватные файлы не использовались, внешних запросов нет. Контракты —
`docs/ALLOCATION.md` §4, `docs/ADMIN.md` §5.

## 1. Дефект: воспроизведение на базовом коммите

Окно списка — 200 операций публичного контура. Синтетическая база: к 16 операциям
периода 2025-07 добавлены 250 более новых операций публичного контура (2026-01),
так что `tx_supplies` (2025-07-08) выходит за окно — ровно та ситуация, в которой
на боевом store оказались операции 2026-10-07 и 2026-04-06.

```console
$ git worktree add --detach <dir> 0bfdfa7      # база карточки
$ PYTHONPATH=. python3 -W error::ResourceWarning <транскрипт из §5>
list window: 200 rows | tx_supplies listed: False
desk.money_card(tx_supplies): None
report 2025-07 before: expense=6508900 excluded_operations=0
GET  /admin/money/tx_supplies -> 404 | <h1>Доступ закрыт</h1><p>Операция не найдена в публичном контуре.</p>
POST /admin/actions/report/exclude -> unreachable: the card page carries no CSRF token to act with
source row: {'amount_kopecks': 250000, 'deleted': 0, 'category_id': 'cat_supplies'}
```

То есть на базе: карточка — `404`, действия недостижимы (страницы, с которой можно
исключить операцию, попросту нет), `desk.exclude_operation` на эту операцию
поднимает `DeskError` «операция не найдена в публичном контуре». Причина одна —
поиск по выдаче списка, ограниченной `MONEY_LIMIT = 200`, а не по идентификатору.
Карточка t_6a308cd3 нашла то же на копии боевого store; `docs/TEST_RESULTS_RELEASE.md`
§6 фиксирует это как ограничение и выносит расширение отдельной задачей — этой.

Новый тест на той же базе (файл тестов скопирован в worktree базы):

| Прогон | Результат |
| --- | --- |
| `python3 -W error::ResourceWarning -m unittest discover -s tests -t .` на `0bfdfa7` | **697 OK** (база без новых тестов) |
| то же на `0bfdfa7` + новые тесты `tests/test_allocation_desk.py` | **700 ran, FAILED (failures=2)** |

Оба падения — новые тесты, и оба падают там, где ожидается: карточка недостижима
(`AssertionError: unexpectedly None`) и отказ по неподтверждённой принадлежности
отдаётся как `404` вместо `409` (`AssertionError: 200 != 404`), потому что
операции для проверки принадлежности стол тоже не находит. Третий новый тест —
охранный (контур при поиске по идентификатору) — проходит и до правки, как и
должен.

## 2. Что изменено

| Файл | Изменение |
| --- | --- |
| `randreport/admin/desk.py` | Текст запроса вынесен в `_MONEY_SELECT`, построение строки карточки — в `_money_view` (та же проверка счётов контура), индекс публичного контура — в `_money_index`; добавлен `money_row(conn, transaction_id)` — прямой поиск `WHERE t.deleted = 0 AND t.transaction_id = ?` с той же проверкой контура; `money_card` теперь делегирует ему. `money_rows` сохранён как список (окно `MONEY_LIMIT` — про список, а не про доступ к одной операции) |
| `tests/test_allocation_desk.py` | Новый класс `OutOfWindowCardTests` (3 теста) |

Контур **не расширен**: строка, у которой хотя бы один счёт вне публичного контура
(личный, карантинный), по-прежнему невидима и по идентификатору — проверка та же
самая (`accounts ⊆ public_accounts`), просто применённая к одной строке. Окно
списка (`MONEY_LIMIT`) не менялось, добавления в публичный payload нет,
`finance_transactions` не меняется.

## 3. Прогоны

| Прогон | Команда | Результат |
| --- | --- | --- |
| Полный набор, python3.13.5 | `python3 -W error::ResourceWarning -m unittest discover -s tests -t .` | **700 OK** (697 прежних + 3 новых) |
| Полный набор, python3.14.9 | `python3.14 -W error::ResourceWarning -m unittest discover -s tests -t .` | **700 OK** |
| Карточный модуль | `python3 -m unittest -v tests.test_allocation_desk` | 9 OK |
| Компиляция | `python3 -m compileall -q randreport tests` | код 0 |
| Синтетическая сборка | `python3 -m tests.dev_release <dir> --period 2025-07` | `published: true`, `release_id: rel_20bebe403c38df58` |
| Публичная проверка | `RANREPORT_RELEASE_DIR=<dir> python3 -m randreport.web check --db /nonexistent` | `pages_checked: 120`, `problem_count: 0` |

Страж ресурсов активен: «resource guard: no unclosed sqlite connection, listening
socket or http response» — без предупреждений на обоих интерпретаторах.

## 4. Карта карточки → тесты

| Пункт приёмки | Тест |
| --- | --- |
| 1. Операция, отсутствующая в последних 200, открывается в столе и исключается | `OutOfWindowCardTests.test_an_operation_outside_the_list_window_opens_and_is_excluded` (окно действительно не содержит её: `assertNotIn` + размер списка = `MONEY_LIMIT`; карточка `200` и те же сумма/дата/категория) |
| 2. Сумма уходит из отчёта **ровно** на величину операции и возвращается `report/restore` | Там же: `expense -= 250_000`, `allocation_excluded_operations = 1`, после возврата — прежний `release_id` и прежняя сумма |
| 3. Источник и `finance_report_exclusions` как прежде (обратимость, аудит, `409`) | Там же: `finance_transactions` не изменён (сумма, `deleted`, категория); `report.operation.restored` возвращает сумму; `test_an_out_of_window_operation_of_an_unconfirmed_category_is_refused` — `409` и фраза «принадлежность», таблица исключений пуста |
| Контур не расширен поиском по идентификатору | `test_a_lookup_by_identifier_never_widens_the_contour`: личный счёт, карантинный счёт и несуществующий id — `money_card` = `None`, карточка `404` |
| 4. Тесты в `tests/test_allocation_desk.py` + весь набор на 3.13.5/3.14.9 под `-W error::ResourceWarning` | §1 и §3 |
| 5. `web check` без проблем | §3 |

Прежние пункты приёмки t_6a308cd3 (без сессии/CSRF `403`, исключение
неподтверждённой категории `409`) не затронуты: шлюз авторизации и проверка
принадлежности не менялись, оба по-прежнему покрыты
`ReversibleExclusionTests`.

## 5. Поведение стола: транскрипт до и после

Транскрипт (синтетика) печатает окно списка, карточку, сумму отчёта до и после
исключения, дельту, возврат и первоисточник.

```console
$ PYTHONPATH=. python3 -W error::ResourceWarning <транскрипт>          # 0bfdfa7
list window: 200 rows | tx_supplies listed: False
desk.money_card(tx_supplies): None
report 2025-07 before: expense=6508900 excluded_operations=0
GET  /admin/money/tx_supplies -> 404 | Операция не найдена в публичном контуре.
POST /admin/actions/report/exclude -> unreachable: the card page carries no CSRF token to act with
source row: {'amount_kopecks': 250000, 'deleted': 0, 'category_id': 'cat_supplies'}

$ PYTHONPATH=. python3 -W error::ResourceWarning <транскрипт>          # с правкой
list window: 200 rows | tx_supplies listed: False
desk.money_card(tx_supplies): {'occurred_on': '2025-07-08', 'amount_kopecks': 250000, 'category_key': 'cat_supplies'}
report 2025-07 before: expense=6508900 excluded_operations=0
GET  /admin/money/tx_supplies -> 200
POST /admin/actions/report/exclude -> 303
report 2025-07 after:  expense=6258900 excluded_operations=1
delta: -250000
POST /admin/actions/report/restore -> 303 | expense=6508900
source row: {'amount_kopecks': 250000, 'deleted': 0, 'category_id': 'cat_supplies'}
```

## 6. Границы

* Окно **списка** осталось 200: список — это список, и он не расширяется. Доступна
  именно одна операция по её идентификатору.
* Идентификаторы и суммы остаются в приватном store: стол по-прежнему loopback и
  отвечает `403` без сессии/CSRF; в публичный релиз ничего нового не попало
  (`web check` 120 страниц, 0 проблем).
* Поиск по идентификатору не является обходом контура: личный/карантинный счёт даёт
  `404`, как и раньше (защита от IDOR сохранена).
