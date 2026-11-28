# Доказательства: подсказки категорий и объяснение распределения на страницах листка

Карточка **t_7fad32c2** («Показать подсказки категорий и объяснение распределения
на страницах листка — после слияния веток»). База — `main` после слияния
`05cc712` и выпуска `03e34a7`. Всё на синтетике: приватный store, реальные данные
и внешние запросы не использовались.

## 1. Что уже было и что пришлось исправить

Слияние `05cc712` встроило в листок две вещи: подсказку в ячейке категории
(`hints.hint_disclosure` в `render._category_cell`, страница «Деньги» и таблица
поддержки) и блок объяснения распределения (`hints.allocation_block`, сразу под
суммами категорий). Проверка на живом адресе и на синтетике показала два дефекта,
которые карточка и закрывает:

1. **Две денежные конвенции на одной странице.** Блок объяснения печатал суммы
   через `format_rub` — `90000.00 ₽`, тогда как весь листок печатает
   `90 000,00 ₽` (`render.rub_from_kopecks`). На боевой странице
   `money?period=2026-11` это было видно рядом: «90 000,00 ₽» в таблице категорий
   и «90000.00 ₽» в блоке аренды. Форматер теперь один: правило живёт в
   `randreport/web/hints.py:rub_from_kopecks`, а `render.rub_from_kopecks`
   делегирует туда, поэтому второй конвенции появиться негде (тест
   `InertAllocationTests.test_the_two_money_renderers_agree`).
2. **У подсказки не было ни одной строки стилей.** Раскрытие рисовалось нативным
   `<details>` без оформления: не читалось как управляемый элемент и не имело
   подписанного контраста. Добавлены правила `.hint-disclosure`, `.hint-text`
   и оформление таблицы аренды тем же набором мебели, что и остальные таблицы
   листка (`<div class="table-wrap">`, `<caption>`, `<th scope>`).

Плюс к этому таблица расчётной аренды получила `scope`-заголовки и `td.num`
вместо `class="data"`, которого в CSS нет вовсе.

## 2. Изменённые файлы

| Файл | Что изменилось |
| --- | --- |
| `randreport/web/hints.py` | `rub_from_kopecks` (русский формат) вместо `format_rub`; таблица аренды в мебели листка; таблица печатается до абзаца-итога |
| `randreport/web/render.py` | `rub_from_kopecks` делегирует в `hints` (реализация одна) |
| `randreport/web/static/site.css` | `.hint-disclosure`, `.hint-disclosure summary`, `.hint-text` (+`:hover`, `[open]`) |
| `tests/test_web_hints_allocation.py` | новый модуль, 26 тестов |

Новых пользовательских текстов не добавлено ни одного: переставлены существующие
абзацы и заменена разметка. Приватный контур, финансовое ядро
(`domain/`, `projection/`, `ops/`, `store/`), CSP и стол не менялись.

## 3. Прогоны

| Прогон | Команда | Результат |
| --- | --- | --- |
| Полный набор, python3.13.5 | `python3 -W error::ResourceWarning -m unittest discover -s tests -t .` | **697 OK** (671 прежних + 26 новых) |
| Полный набор, python3.14.9 | `python3.14 -W error::ResourceWarning -m unittest discover -s tests -t .` | **697 OK** |
| Новый модуль | `python3 -m unittest -v tests.test_web_hints_allocation` | 26 OK |
| Компиляция | `python3 -m compileall -q randreport tests` | код 0 |
| Синтетическая сборка с подсказками и правилами | `python3 build_hint_release.py <dir>` (фикстуры + `ReleaseBuilder(policy=…)`) | `published: true`, `rel_0f57f4fb433479cd` |
| Публичная проверка | `RANREPORT_RELEASE_DIR=<dir> python3 -m randreport.web check --db /nonexistent` | `pages_checked: 120`, `problem_count: 0` |

Страж ресурсов молчит: «resource guard: no unclosed sqlite connection, listening
socket or http response».

## 4. Что именно проверяет новый модуль

| Пункт карточки | Тест |
| --- | --- |
| Утверждённая подсказка отрисована рядом со своей категорией | `ApprovedHintTests.test_an_approved_hint_is_rendered_next_to_its_own_category` |
| Отсутствующая подсказка не выдумывается, пустого раскрытия нет | `...test_a_category_without_an_approved_hint_gets_no_disclosure` |
| Подсказка едет с категорией на другую страницу (там, где есть категории) | `...test_the_support_page_carries_the_hint_of_a_category_it_renders` |
| Текст подсказки не попадает на страницу вне раскрытия | `...test_the_hint_text_is_never_rendered_outside_its_disclosure` |
| Нативный `<details>/<summary>`, без скриптов и `onclick` | `...test_the_disclosure_is_a_native_summary_that_needs_no_script` |
| Скринридер читает подсказку с именем категории (`aria-label`) | `...test_the_screen_reader_reads_the_hint_with_its_category_name` |
| Объяснение собрано из реального `AllocationDTO` релиза | `AllocationExplanationTests.test_the_explanation_is_built_from_the_real_allocation_dto` |
| Блок стоит рядом с суммами категорий, до архива | `...test_the_explanation_sits_next_to_the_category_totals` |
| Одна денежная конвенция; второй («90000.00 ₽») на странице нет | `...test_the_allocation_money_uses_the_leaflet_convention` |
| Мебель таблиц листка | `...test_the_explanation_carries_the_leaflet_table_furniture` |
| Расчётная аренда и банковский платёж — рядом, без двойного учёта | `...test_the_explanation_states_the_rent_and_the_bank_payment_apart` |
| Публичный `category_key` правил на странице не появляется | `...test_the_explanation_never_publishes_an_allocation_key` |
| Отсутствие приватных строк: те же шаблоны, что у `web check`, плюс явный список id/ключей и внутренних слов-ведров | `NoLeakTests.*` |
| CSP не ослаблен, `script-src` не добавлен | `NoLeakTests.test_the_content_security_policy_is_the_same_and_still_blocks_scripts` |
| Стили раскрытия и видимая обводка фокуса | `HintStylesheetTests.*` |
| Контраст AA посчитан по значениям из отданного CSS (≥ 4.5:1) | `HintStylesheetTests.test_the_colours_the_disclosure_uses_pass_contrast_aa` |
| Враждебная подсказка экранируется и не ломает разметку | `HostileHintTests.*` |
| Нереализованное правило не рисует блок | `InertAllocationTests.test_an_absent_or_inert_allocation_renders_nothing` |
| Неразнесённое не превращается в ноль рядом с блоком | `InertAllocationTests.test_the_block_never_turns_an_unallocated_part_into_a_zero` |
| Два денежных рендерера совпадают | `InertAllocationTests.test_the_two_money_renderers_agree` |

Контраст (посчитан по фактическим токенам из `site.css`, нормальный текст — порог
4.5:1): `--plum-2` на `--sheet` **8.85:1**, `--ink-2` на `--sheet` **10.05:1**,
`--rust` (наведение) **6.51:1**.

## 5. Живая проверка в браузере (синтетический staging, loopback)

Страница `money?period=2025-07` с четырьмя утверждёнными подсказками и
применёнными правилами.

* **Скринридер.** `Accessibility.getFullAXTree` отдаёт три узла
  `DisclosureTriangle` с именами `Подсказка: Аренда`, `Подсказка: Расходники`,
  `Подсказка: Коммунальные`; строка таблицы читается как
  «Аренда Подсказка: Аренда содержание пространства».
* **Клавиатура.** Порядок `Tab` (записан по шагам): 1–14 — ссылки и выбор
  периода, **15, 16, 17 — три `summary` подсказок**, 18 — ссылка CSV, далее
  подвал. `Enter` и `Space` на сфокусированном `summary` раскрывают подсказку
  (проверено `details.open`), курсор фокуса даёт видимую обводку 3 px
  (`summary:focus-visible`).
* **Мышь и касание.** Щелчок по `summary` и `Input.dispatchTouchEvent`
  (touchStart/touchEnd) раскрывают ту же подсказку.
* **Вёрстка.** Горизонтальной прокрутки нет: 1440 → `scrollWidth` 1425,
  390 → 390. Таблица аренды на 390 влезает целиком, суммы не обрезаны.

## 6. Артефакты

* `/home/agent/.hermes/cache/scratch/rr/hint-shots/money_1440.png` — «Деньги»,
  1440 px, подсказка «Аренда» раскрыта.
* `/home/agent/.hermes/cache/scratch/rr/hint-shots/money_390.png` — «Деньги»,
  390 px, таблица категорий с раскрытой подсказкой.
* `/home/agent/.hermes/cache/scratch/rr/hint-shots/money_1440_rules.png`,
  `money_390_rules.png` — блок «Правила распределения подвала» с таблицей
  «Месяц / Расчётная аренда / Банковский платёж».
* `/home/agent/.hermes/cache/scratch/rr/hint-shots/support_390.png` — страница
  «Поддержка», 390 px: подсказка стоит у своей категории и на второй странице.
* `/home/agent/.hermes/cache/scratch/rr/hint-shots/money_1440_full.png` — полная
  страница 1440 px.

## 7. Состояние боя и остаток

Боевой адрес `https://rand.185.121.13.113.sslip.io` уже отдаёт подсказки и блок
распределения (проверено `curl`): `money?period=2026-11` — пять раскрытий
`<details class="hint-disclosure">` с утверждёнными владельцем текстами, блок
«Правила распределения подвала» с расчётной арендой 90 000 ₽ и банковским платежом
25 000 ₽. Дефект форматирования там ещё виден (два «90000.00 ₽» против одного
«90 000,00 ₽») — он уходит с ближайшим выпуском; отдельной карточкой вынесен
выпуск исправленного листка, чтобы правка не осталась только в репозитории.
