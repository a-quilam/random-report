# Доказательства: правила распределения подвала и обратимое исключение операции

Карточка: **t_90caf8e5** — «Правила категорий подвала и обратимое исключение
операции редактором». Ветка `revision/allocation`, база `087c5b2`. Всё на
синтетике: реальные данные, секреты и приватные файлы не использовались,
внешних запросов нет. Контракт — `docs/ALLOCATION.md`, решение — `docs/DOMAIN.md`
§11 (17).

## 1. Сводка прогонов

| Прогон | Команда | Результат |
| --- | --- | --- |
| Полный набор, python3.13.5 | `python3 -W error::ResourceWarning -m unittest discover -s tests -t .` | **645 OK** (614 прежних + 31 новых) |
| Полный набор, python3.14.9 | `python3.14 -W error::ResourceWarning -m unittest discover -s tests -t .` | **645 OK** |
| Новые правила (домен) | `python3 -m unittest -v tests.test_allocation_rules` | 25 OK |
| Новые правила (стол) | `python3 -m unittest -v tests.test_allocation_desk` | 6 OK |
| Компиляция | `python3 -m compileall -q randreport` | код 0 |
| Синтетическая сборка | `python3 -m tests.dev_release <dir> --period 2025-07 --db <db>` | `published: true`, `release_id: rel_20bebe403c38df58` |
| Публичная проверка | `RANREPORT_RELEASE_DIR=<dir> python3 -m randreport.web check --db /nonexistent` | `pages_checked: 120`, `problem_count: 0` |

Страж ресурсов активен: «resource guard: no unclosed sqlite connection,
listening socket or http response» — без предупреждений.

## 2. Карта карточки → тесты

| Пункт карточки | Тест |
| --- | --- |
| Исключённая категория не влияет даже на общие суммы | `ExcludedCategoryTests.test_an_excluded_category_leaves_no_trace_in_any_total` |
| Треть для общих расходов и ЖКУ, месячная агрегация целых копеек, определённое округление | `ShareOfSharedExpensesTests.test_the_fraction_is_applied_to_each_month_not_to_the_year`, `...test_the_rounding_rule_is_explicit_and_changeable` |
| Год из месяцев | `ShareOfSharedExpensesTests.test_the_year_is_the_sum_of_its_rounded_months` |
| Фиксированные 90000 ₽/мес, без двойного учёта | `RentTests.test_the_agreed_amount_replaces_the_payment_without_double_counting` |
| Без заднего переписывания закрытых отчётов/неизвестных периодов | `RentTests.test_a_month_before_the_rule_keeps_the_actual_payment`, `...test_a_month_without_any_rent_is_not_given_an_invented_amount`, `PolicyImportTests.test_a_policy_change_never_rewrites_a_frozen_report_or_a_balance` |
| Явное различие расчётной аренды и банковского платежа | `RentTests.test_the_agreed_rent_and_the_bank_payment_are_distinct_numbers` |
| Корректировки обоих знаков отдельно | `CorrectionTests.test_both_signs_are_kept_separately_and_an_unknown_article_stays_unclassified` |
| Неопределённая статья без выдуманного класса | `CorrectionTests.test_an_unclassified_correction_gets_no_invented_class` |
| Надкатегория не задваивает дочерние статьи | `SupercategoryTests.test_a_supercategory_does_not_double_its_children`, `...test_a_parent_carries_only_the_part_its_children_do_not_cover` |
| Обратимое «Исключить из отчёта»/вернуть с CSRF, аудитом, неизменными первоисточниками | `ReversibleExclusionTests.test_excluding_one_operation_needs_csrf_and_is_reversible` |
| Защита при неизвестной принадлежности | `ReversibleExclusionTests.test_an_operation_of_an_unconfirmed_category_is_protected` |
| Соответствие агрегатов, CSV/JSON, HTML, пояснения; безопасные DTO | `PublicConsistencyTests.test_json_csv_and_html_state_the_same_amounts`, `...test_the_allocation_explanation_is_a_safe_dto`, `...test_an_empty_allocation_is_an_inert_allocation_dto` |
| Изменение политики не переписывает остатки | `BalanceTests.test_a_policy_change_never_rewrites_a_balance`, `PolicyImportTests.test_a_policy_change_never_rewrites_a_frozen_report_or_a_balance` |
| Свободный текст оператора не публикуется | `CorrectionTests.test_the_operators_free_text_reason_is_never_published` |
| Утверждённые публичные названия + необязательные подсказки доступны touch/keyboard | `HintAccessibilityTests.test_an_approved_hint_is_reachable_by_touch_and_keyboard`, `...test_a_category_without_an_approved_hint_gets_no_invented_hint` |
| Формат политики и импорт оператором | `PolicyPayloadTests.*`, `PolicyImportTests.test_an_imported_policy_is_applied_by_the_next_build`, `...test_a_broken_policy_is_refused_with_a_human_sentence`, `...test_the_settings_page_shows_the_rules_and_the_exclusions` |

## 3. Ключевые числа (синтетика)

* **Треть, месячная агрегация против годовой.** Два месяца по 100001 коп.
  (ЖКУ): `1/3` с `half_up` → 33334 + 33334 = **66668**; доля годового итога
  (200002/3 = 66667.33 → 66667) дала бы другое число. Тест фиксирует именно
  месячный результат, а годовой релиз равен сумме месячных.
* **Округление.** 100000 коп. → `floor` 33333, `ceil` 33334, `half_up`
  33333 (1/3 = 33333.33). Округление задаётся явно в правиле; неизвестное —
  отказ (`AllocationError`).
* **Аренда.** Январь 4 000 000 + февраль 4 500 000 (факт), использование
  9 000 000/мес → в расходах **18 000 000**; фактический платёж **8 500 000**
  показан рядом и в итог не добавлен (`rent_differs = true`).
* **`from_month`.** Правило с `from_month = 2026-02`: январь сохраняет 4 000 000,
  февраль заменяется на 9 000 000, расчётная аренда 9 000 000.
* **Корректировки.** +5000 и −2000 по подтверждённой статье и +300 по
  неизвестной: положительные 5300, отрицательные −2000 (раздельно);
  расходы 103000, неопознанное +300 остаётся `unclassified`.
* **Надкатегория.** Родитель 1 000 000, дети 400 000 + 600 000: без правила
  2 000 000, с правилом **1 000 000** (родитель не задваивает детей).
* **Стол.** Исключение `tx_supplies` (250 000) уменьшает расход за 2025-07 ровно
  на 250 000; после «вернуть» проекция контентно совпадает с исходным релизом
  (`rel_…` до и после равны), первоисточник (`amount_kopecks`, `deleted`,
  `category_id`) не изменён.

## 4. Поведение стола (транскрипты)

* Без CSRF: `POST /admin/actions/report/exclude` → **403** («Токен CSRF не
  совпадает»), исключения не появляется.
* С CSRF: **303** на возврат, запись в `finance_report_exclusions`, строка в
  `audit_log` (`report.operation.excluded`), карточка показывает «Вернуть в
  отчёт».
* Неподтверждённая принадлежность: `POST /admin/actions/report/exclude` для
  `tx_new_category` (категория вне карты) → **409** «принадлежность операции
  неизвестна: категория не подтверждена, исключить её из отчёта нельзя».
* Импорт политики: `POST /admin/actions/allocation/import` с
  `{"rent": {"monthly_kopecks": 1.5}}` → **409** «rent.monthly_kopecks: ожидается
  целое число копеек» (политика не записана).
* Закрытый период: после `close_period` правка политики и пересборка дают
  **тот же** `report_snapshots` (версий по-прежнему 1), а снимки баланса не
  меняются.

## 5. Файлы

Новые: `randreport/domain/allocation.py`, `randreport/web/hints.py`,
`tests/test_allocation_rules.py`, `tests/test_allocation_desk.py`,
`docs/ALLOCATION.md`.
Изменённые: `randreport/domain/finance.py` (поля `category_hints`/`allocation` в
`ScopePolicy`, подсказка в строке категории), `randreport/domain/dto.py`
(`AllocationDTO`, `RentMonthDTO`, `AllocationAdjustmentDTO`, `hint` у категории,
вложенный `allocation` в `MoneyDTO`), `randreport/projection/policy.py` (разбор
`allocation`, подмешивание активных исключений), `randreport/projection/release.py`
(метрики и DTO распределения), `randreport/ops/pipeline.py` (применение правил до
итогов), `randreport/store/migrations.py` (миграция 7), `randreport/store/repository.py`
(обратимые исключения), `randreport/admin/desk.py` (+`app.py`, `render.py`),
`randreport/web/render.py`, `docs/DOMAIN.md` §11 (17), `docs/PROGRESS.md`,
`tests/test_e02_opaque_revision.py` (набор применённых миграций `(4,5,6,7)`).

## 6. Границы и внешние входы

* Проверено только на синтетике. Реальный импорт владельческой политики, реальные
  суммы и боевой ночной прогон с этими правилами — за оператором (карточка
  `t_6a308cd3`, закрытый staging).
* Публичная переработка страниц (§5 `docs/ALLOCATION.md`, доступность подсказок
  касанием/клавиатурой, редакторская вёрстка) — карточка `revision/visual`
  (`t_6ef9ffd6`); здесь зафиксирован контракт, DTO и рабочий образец разметки.
* Удаление/переписывание уже опубликованных архивных версий не выполняется: это
  запрещено неизменяемостью `report_snapshots`.
