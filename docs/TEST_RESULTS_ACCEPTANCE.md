# Независимая приёмка, восстановление и измерение staging

Карточка: `t_7b6695ce` — «Независимая приёмка, восстановление и измерение staging».
Матрица: полная `docs/REQUIREMENTS.md` §13 (`F01–F07, S01–S03, E01–E03, T01,
N01–N02, H01, A01–A02, R01–R02, U01, O01`). Родитель: `t_0e30c3f2` (закрытый
редакционный стол), дочерняя: `t_1b5b08c8` (боевой запуск).

Проверялись **только код и синтетические данные**. Ни один платный сервис не
подключался, ни один реальный источник не опрашивался, посторонние службы
машины не менялись, remote не настраивался (всё локально).

## 1. Окружение и команды

```
$ python3 -V
Python 3.13.5
$ python3 -m unittest discover -s tests -t .
Ran 348 tests in 9.3s
OK
$ python3 -W error::ResourceWarning -m unittest discover -s tests -t .
Ran 348 tests in 9.3s
OK                                # предупреждения как ошибки: незакрытых дескрипторов нет
$ python3 -m unittest discover -s tests -t .       # текущее дерево (карточка t_c3aa8c3e)
Ran 363 tests in 9.4s
OK
resource guard: no unclosed sqlite connection, listening socket or http response
$ python3 -m compileall -q randreport tests
(без вывода)
$ python3 -m randreport.web check
{"pages_checked": 120, "problems": [], "problem_count": 0}
$ systemd-analyze calendar '*-*-* 03:00:00 Europe/Moscow'
Normalized form: *-*-* 03:00:00 Europe/Moscow
    Next elapse: Mon 2026-11-23 01:00:00 CET
       (in UTC): Mon 2026-11-23 00:00:00 UTC
```

323 теста пришли от карточек домена/сбора/сайта/стола; 25 новых — из этой
проверки (`tests/test_acceptance_independent.py`), 15 — из карточки
`t_c3aa8c3e` (`tests/test_resource_guard.py`), 363 всего. Модули и объёмы:

```
 38 a01_admin_auth   20 a02_admin_verification   25 acceptance_independent
  2 admin_e2e         8 e01_completed_events       9 e02_occurrences
  9 e02_teamup_sync  16 e03_r02_privacy           11 f01_f02_formulas
 15 f03_f04_classification  9 f05_reconciliation   7 f06_precision
 10 f07_balance       3 finance_integrity_properties  17 h01_admin_history
  6 history_archive  25 n01_n02_news              16 r01_admin_publication
 15 resource_guard   14 s01_zenmoney_sync          5 s02_resilience
 14 s03_scheduler    10 store_migrations          10 t01_telegram
 49 web_public_site
```

## 2. Что найдено и исправлено

Проверка велась от попытки **сломать** инвариант, а не от «тесты прошли».
Найдено четыре дефекта и один пробел; все исправлены в этом же коммите.

### 2.1. `CSV-инъекция формул через подпись категории` — исправлено

Публичный CSV (`/api/public/export/categories.csv`) писал `category_label` как
есть. Подпись категории берётся из источника (Zenmoney), поэтому значение вида
`=HYPERLINK("http://evil.example","x")`, `=cmd|calc`, `+1+1`, `@SUM(1+1)` при
открытии файла в таблице выполнялось как формула. До правки:

```
'=HYPERLINK("http://evil.example","x")' -> '=HYPERLINK(""http://evil.example""…'
'+1+1' -> '+1+1'     '@SUM(1+1)' -> '@SUM(1+1)'     '=cmd|calc' -> '=cmd|calc'
```

Исправление (`randreport/projection/csv_export.py`, `_csv_cell`): ячейка, первый
непробельный символ которой `= + - @ \t \r`, получает префикс `'` (литеральный
текст для таблицы), при этом настоящие отрицательные числа (`-40000.00`) остаются
числами. Тесты: `test_csv_neutralises_spreadsheet_formula_triggers`,
`test_csv_keeps_real_negative_numbers_numeric`.

### 2.2. `API отдал 500 вместо 400 на id-подобное имя параметра` — исправлено

`GET /api/public/money?period=2025-07&acc_card=x` возвращал `500`
(«Внутренняя проверка приватности остановила выпуск»), тогда как HTML-путь того же
запроса — `400`. Причина: тело *ошибки* прогонялось через тот же guard приватности,
что и данные, а сообщение содержит имя параметра, введённое вызывающим:
`parameters outside the public allowlist: ['acc_card']` — guard распознавал текст
вызывающего как утечку и заменял корректный `400` на `500`. Это нарушает
контракт `docs/WEB.md` §3 («любой другой параметр … отклоняются с 400 до чтения
данных»).

Исправление (`randreport/web/app.py`): у `_json` появился флаг `guarded`
(по умолчанию `True` — для данных), `_json_error` строит тело без guard'а
(тело ошибки — это эхо запроса вызывающего, не внутренние данные). После правки:
`acc_card`, `tx_1`, `occurrence_id=occ_9`, `calendar_id=cal_1` → `400`; обычные
ответы по-прежнему `200`, неизвестный endpoint — `404`. Тест:
`test_api_rejects_an_id_like_parameter_with_400_not_500`.

### 2.3. `Панель «Публикация» падала 500 из-за группового периода` — исправлено

Самая дорогая находка. После `python -m randreport.web seed` (штатный шаг) в
`report_snapshots` лежит групповой отчёт с меткой `2025-01..2025-12`. `_period_index`
передавала метку в `reports.publication_state()` → `readiness()` →
`draft_payload()` → `plane_for()`, а тот звал `parse_period()`, который диапазоны
**не принимает**:

```
UnsafePeriodError: unsupported public period '2025-01..2025-12'
  → PublicationError: некорректный период отчёта
  → /admin/publish        → 500
  → /admin/publish/2025-07 → 500   (индекс периодов строится и на этой странице)
```

То есть на корректно засеянной staging-БД раздел «Публикация» был недоступен
целиком. Существующие тесты этого не ловили: их fixtures не сеют групповые
снимки, а E2E открывал только `/admin/publish/2025-07` до посева.

Исправление (`randreport/admin/reports.py`): `parse_report_label()` понимает
`YYYY`, `YYYY-MM` и диапазон `YYYY-MM..YYYY-MM` (через `range_period`);
`plane_for` использует его — групповой отчёт корректно опознаётся как
историческая плоскость (`history.exact_report` уже умеет диапазоны). Дополнительно
`_period_index`/`_publish` больше не могут уронить всю страницу одной нечитаемой
меткой: такая метка попадает в список («период не распознан»), а одиночная
страница отдаёт честный `409` с человеческой причиной. После правки закрытая staging-копия:
`/admin/publish` → `200`, `/admin/publish/2025-07` → `200`,
`/admin/publish/2025-01..2025-12` → `200` («Черновик поста за 2025-01..2025-12»,
плоскость `historical`). Тест: `PublishPanelTests`.

### 2.4. `javascript:`-схема в публичных ссылках (уплотнение) — исправлено

`href` из данных (`report.url`, `item.source_urls`, ссылка версии) подставлялся с
экранированием, но без проверки схемы: `javascript:alert(1)` превращался в живую
ссылку. Сейчас URL из источника возможен только как `https://t.me/…`
(собирается кодом), так что эксплуатируемого пути нет, но это лишний шаг до XSS,
если источник ссылок когда-либо расширят. Добавлен `render.safe_url()`: только
`http`/`https` или свой путь (`/…`, но не `//host`). Тесты:
`test_safe_url_allows_only_http_https_and_local_paths`,
`test_html_escapes_untrusted_text`.

### 2.5. Пробел `O01`: не было эксплуатационного бэкапа — закрыт

Требование §13 `O01` и §9 («резервные копии … с проверяемым восстановлением,
консистентный SQLite backup API») в коде закрывались только внутри теста E2E;
команды и процедуры восстановления на чистом экземпляре не существовало. Добавлен
`randreport/ops/backup.py` и команды `ops.cli backup | restore | verify-backup`
(см. §4).

### 2.6. Утечки ресурсов (`ResourceWarning`) — исправлено

По замечанию ревью прогнал suite с `-W always::ResourceWarning`: **102**
предупреждения `unclosed database` и 3 `unclosed <socket>` — то есть приложение и
фикстуры оставляли незакрытые дескрипторы (в долгоживущем процессе это утечка
файловых дескрипторов, а не косметика). Источники и правки:

* служебные CLI (`randreport/web/__main__.py`, `randreport/admin/__main__.py`,
  `randreport/ops/cli.py`) открывали соединение и никогда не закрывали его —
  `_open_db`/`_serve`/`_served_site` стали контекстными менеджерами
  (`with` + `finally: conn.close()`), а `admin serve` закрывает и соединение стола;
* `randreport/admin/app.py`: `AdminDesk.close()` вызывался из главного потока, а
  соединение создавалось в потоке запроса, поэтому `sqlite3` бросал
  `ProgrammingError`, который глушился — и дескриптор **не закрывался**. Теперь
  стол открывает соединение с `check_same_thread=False`
  (`store/db.connect(..., check_same_thread=...)`, документировано): одно общее
  соединение — уже проектное решение, а закрытие из другого потока перестало
  ломаться;
* `randreport/web/app.py`: команды `web check/export/seed/correct/build` больше не
  бросают открытый read-only коннектор (`site.close()` в `finally`);
* тестовые фикстуры и setUps: `fixtures_admin._template_root()`,
  `test_s01/setUp`, `test_a01` (×2), `test_a02`, `test_h01`, `test_r01`,
  `test_admin_e2e` (×2) закрывают соединения через `addCleanup(self.conn.close)`;
* `test_admin_e2e`: `make_server(...).server_close()` (было только `shutdown()`,
  слушающий сокет оставался) и `HTTPError.close()` в HTTP-клиенте.

Проверка: `python3 -W error::ResourceWarning -m unittest discover -s tests -t .`
→ `Ran 348 tests … OK` (предупреждения как ошибки, ни одного не осталось).
Подавления (`filterwarnings`, `simplefilter`) не добавлялись.

#### 2.6.1. Независимая перепроверка и разделение «приложение / тесты»

Карточка `t_c3aa8c3e` воспроизвела дефект на прежнем HEAD `01e2080` и проверила
исправление на текущем дереве — одной и той же командой карточки
(`python3 -W always::ResourceWarning -m unittest discover -s tests -q`), двумя
интерпретаторами, что важно: 3.14 добавляет свои предупреждения.

| дерево | интерпретатор | тесты | `ResourceWarning` |
|---|---|---|---|
| `01e2080` | 3.13.5 | 348 OK | **105**: 102 `unclosed database` + 3 `unclosed <socket>` |
| `01e2080` | 3.14.9 | 348 OK | **118**: те же 105 + 13 `Implicitly cleaning up <TemporaryDirectory>` |
| текущее | 3.13.5 | 363 OK | 0 |
| текущее | 3.14.9 | 363 OK | 0 |

Число из карточки (221) — это строки `ResourceWarning: …` в прогоне на 3.14:
102 `unclosed database` + 3 `unclosed <socket>` + 13 `Implicitly cleaning up
<TemporaryDirectory>` + 103 подсказки `Enable tracemalloc`, то есть те же 105
реальных дескрипторов плюс временные каталоги фикстур. Счёт «дескрипторов»
(105 / 118) точнее: подсказка tracemalloc — не отдельный дефект.

Источники (владение ресурсом, а не место предупреждения — предупреждение
печатается там, где сработал GC):

| владелец | незакрытых | где |
|---|---|---|
| `tests/test_a01_admin_auth.py:30` | 33 | `setUp`, соединение из фикстуры |
| `tests/test_a02_admin_verification.py:25` | 20 | `setUp` |
| `tests/test_h01_admin_history.py:27` | 17 | `setUp` |
| `tests/test_r01_admin_publication.py:29` | 16 | `setUp` |
| `tests/fixtures_ingest.py:69` ← `tests/test_s01_zenmoney_sync.py:52` | 10 | `setUp` |
| `tests/fixtures_admin.py:453` | 1 | шаблон БД фикстуры |
| `tests/test_admin_e2e.py` (`:155`, `:178`, `:272`) | 3 | HTTP-клиент E2E |
| `tests/test_admin_e2e.py` (`:103`, `:104`, `:254`) | 3 сокета | `make_server` без `server_close()` |
| **`randreport/admin/app.py:110`** | **1** | **приложение**: соединение стола, закрытие из чужого потока |

То есть 104 из 105 — тестовые фикстуры, 1 — приложение (`AdminDesk`); отдельная
ветка утечек — служебные CLI, которых suite в процессе не запускает, см. ниже.

Важный вывод для приёмки: `-W error::ResourceWarning` на дереве `01e2080` даёт
`Ran 348 tests … OK` и **exit 0**. Предупреждение о незакрытом дескрипторе
возникает в финализаторе, то есть становится *unraisable*, а не ошибкой теста,
поэтому «OK с предупреждениями как ошибками» само по себе ничего не доказывает:
доказывает только счётчик `-W always` или автоматическая проверка (§2.8).

Вторая ветка: на `01e2080` **9 из 12** служебных команд (`web seed`, `web correct`,
`admin bootstrap|member|sessions|history|diagnose|post|close`, `ops.cli status`)
оставляли по одному незакрытому соединению (ровно один `ResourceWarning` на
команду, exit 0) — приложение утекало и вне тестов. Сейчас 12/12 команд → 0
предупреждений, коды возврата как задокументированы (`correct` → 2 «отказ»,
`member` → 2 «нужен ровно один из `--add`/`--remove`»).

### 2.7. Команда `web correct` падала трейсбеком — исправлено

`python -m randreport.web correct --period <нет снимка>` выносила
`ReportError` наружу как traceback. Теперь CLI ловит `ReportError` и печатает
человеческую причину с кодом возврата 2 (`refused: no published snapshot for
period '2024-01'`), как и остальные отказы.

### 2.8. Автоматическая проверка незакрытых дескрипторов перед выходом процесса

Предупреждение само по себе дефект не закрывает: `-W error::ResourceWarning`
пропускает его как unraisable и возвращает 0 (§2.6.1), а без `-W always` оно
вообще не печатается (стандартный фильтр игнорирует `ResourceWarning`). Значит,
дефект возвращается молча. Добавлен `randreport/leakguard.py`:

* `TrackedConnection` (подкласс `sqlite3.Connection`) помнит свой `close()`;
  `__del__` **не** определяется, потому что python-финализатор подкласса
  вытесняет C-финализатор, а именно он печатает `unclosed database`. Закрытость
  читается из факта `close()`, а не угадывается: `__del__`-вариант и
  «пробуем `execute()` из другого потока» дают ложные срабатывания, потому что
  `check_same_thread` бросает `ProgrammingError` и для открытого, и для
  закрытого соединения (эта же двусмысленность и прятала утечку стола);
* реестр держит выданные дескрипторы живыми — иначе отброшенный без `close()`
  дескриптор исчезает до проверки; проверка ничего не закрывает сама;
* `install()` регистрирует проверку в `atexit`: при найденных дескрипторах
  печатается отчёт с сайтом создания, эмитится тот же `ResourceWarning`, что
  напечатал бы CPython при финализации (её пропускает `os._exit`), и процесс
  выходит с кодом 1; при чистом выходе печатается одна строка подтверждения
  (`verbose=True`);
* `install()` вызывается из `tests/__init__.py` (то есть из любой команды
  `unittest discover`) и из `main()` всех трёх CLI (`web`, `admin`, `ops.cli`),
  так что проверка встроена и в эксплуатационный запуск;
* `close_tracked_handles()` — только для тестов, которые намеренно создают
  утечку (свои тесты стража), чтобы она не валила проверку всего suite.

Проверка, что страж действительно ловит (а не «всегда молчит»): на дереве
`01e2080` (до правок) тот же suite даёт `Ran 348 tests … OK`, **exit 1** и отчёт
на 105 дескрипторов с владельцами из §2.6.1; на текущем дереве — exit 0 и строка
подтверждения. То же для CLI: на `01e2080` `web seed` со стражем выходит 1 и
указывает `randreport/web/__main__.py:58` (прежний `_open_db`).

Доказательства (локально, синтетика):

```
$ python3 -W always::ResourceWarning -m unittest discover -s tests -q
Ran 363 tests in 11.3s
OK
resource guard: no unclosed sqlite connection, listening socket or http response
$ python3 -W error::ResourceWarning -m unittest discover -s tests -q
Ran 363 tests in 12.1s
OK
$ python3 -m unittest discover -s tests -q            # 3 повтора подряд
exit=0, предупреждений 0, OK — детерминированно
$ python3 -m randreport.web seed --db /tmp/x.sqlite   # и остальные 11 команд
0 предупреждений, код возврата как задокументирован
```

15 новых тестов (`tests/test_resource_guard.py`) проверяют сам страж: открытое
соединение находится с указанием владельца, закрытое — нет, соединение из чужого
потока считается открытым (та самая ловушка), слушающий сокет находится до
`server_close()`, `install()` идемпотентен и не трогает `warnings.filters`,
отдельный процесс с утечкой выходит 1 (и печатает и отчёт, и `ResourceWarning`),
чистый процесс выходит 0, код выхода настраивается.

## 3. Матрица §13: где проверено

«Где» — модуль/класс, который это доказывает; «Независимо» — что дополнительно
проверено в этой карточке поверх карточек-авторов.

| ID | Критерий §13 | Где проверено | Независимо |
|---|---|---|---|
| F01 | E=100000,C=60000,S=40000: 60 %, shortfall, result_before/after, баланс независимо | `test_f01_f02_formulas` (11) | формулы не пересчитывались заново; проверена их непротиворечивость через повторный прогон |
| F02 | 110 %/excess; E=0, E<0, нет субсидии, нет данных, подтверждённый ноль, отрицательная компенсация | `test_f01_f02_formulas` | повторный прогон 348 тестов |
| F03 | взнос и компенсация раздельны, прямой платёж не удваивается, смешанная категория | `test_f03_f04_classification` (15) | — |
| F04 | переводы внутри/между валютами/вне scope, комиссия, возврат, отмена доната, заём, корректировка, плановая | `test_f03_f04_classification` | на staging в релизе видно: `cat_commission`, `cat_purchase_refund`, `cat_balance_adjustment`, `tx_move` — не попали в публичные строки как операции |
| F05 | новая категория обнаружена; 62819+72000+5750+22400=162969; год не пропускает | `test_f05_reconciliation` (9) | — |
| F06 | копейки, weighted год, totals, январь/декабрь | `test_f06_precision` (7), `test_finance_integrity_properties` (3) | — |
| F07 | баланс из ликвидных публичных счетов; неизвестные счета/валюты | `test_f07_balance` (10) | — |
| S01 | full/diff/повтор payload/edit/delete; курсор после commit; retry не дублирует | `test_s01_zenmoney_sync` (14) | staging: `source_cursors` = `zenmoney 1000`, `teamup …+03:00`, `telegram 701`, `news 701` |
| S02 | ошибка/partial/restart: последний валидный snapshot остаётся | `test_s02_resilience` (5) | бэкап/восстановление сохранили `last_good` и курсоры (§4) |
| S03 | планировщик 03:00 MSK, одновременные запуски, catch-up, идемпотентность | `test_s03_scheduler` (14), `ScheduleTests` (6) | `systemd-analyze calendar`, DST/чужой TZ, `is_due`/`next_run_at`/`missed_logical_dates` |
| E01 | будущее/текущее исключено, end==cutoff включён, ночное/многодневное, all-day, МСК-граница | `test_e01_completed_events` (8) | — |
| E02 | recurring, несколько календарей, перенос, удаление, retention не стирает архив | `test_e02_occurrences` (9), `test_e02_teamup_sync` (9) | staging: 6 occurrences, публичных 2 |
| E03 | закрытое только в агрегатах; ни title/date/id/расписания в HTTP/CSV/static | `test_e03_r02_privacy` (16), `test_web_public_site` (49) | скан 120 страниц → 0 проблем; статический экспорт просканирован целиком |
| T01 | подписчики точки/пропуск; ноль не при ошибке; без интерполяции | `test_t01_telegram` (10) | — |
| N01 | выбор новости из поста, source refs, LLM invalid/injection/timeout/стоимость | `test_n01_n02_news` (25) | — |
| N02 | изменение/удаление поста, ручное скрытие, повторный запуск не плодит | `test_n01_n02_news` | на staging `news_items` = 1 (анонс отброшен) |
| H01 | range, overlap, неоднозначный год, missing/0, конфликты, частичный 2025 | `test_h01_admin_history` (17), `test_history_archive` (6) | staging: 10 наблюдений; групповой отчёт `2025-01..2025-12` (он же вскрыл §2.3) |
| A01 | login подпись/nonce/state/aud/expiry, username spoof, отозванная сессия, CSRF, IDOR, XSS, no leak | `test_a01_admin_auth` (38) | + `SecretHygieneTests`, проверки заголовков по HTTPS (§5) |
| A02 | все коммунары проверяют; инвалидация только затрагиваемого; ручная проверка не устраняет issue | `test_a02_admin_verification` (20) | — |
| R01 | архив snapshot, корректировка с diff и новой версией, исходное не перезаписано, текст не выдумывает | `test_r01_admin_publication` (16), `test_admin_e2e` (2) | восстановление сохранило v1 и v2 приёмки (§4) |
| R02 | CSV только категории/разрешённые периоды, без личных полей при нестандартных параметрах | `test_e03_r02_privacy`, `test_web_public_site` | `account_id`/`transaction_id`/`day`/`unit=USD` → `400`; CSV формула-инъекция закрыта (§2.1) |
| U01 | 360/390/768/1280, клавиатура, фокус, контраст, пустые/неполные состояния, публичное без login | `test_web_public_site` (49) + скриншоты | 29 скриншотов (§6) |
| O01 | restore from backup, миграция на чистой БД, `.env.example`, секретов нет в git/test/log/build, deployment/rollback | **новое:** `BackupRestoreTests` (5), `OperationalCliChainTests` (2), `SecretHygieneTests` (3), `test_store_migrations` (10), `docs/DEPLOYMENT.md` | §4, §5, §7 |

Покрытие: **unit + integration + contract + e2e + property-based (finance) +
реальный loopback HTTP + реальный браузер**; lint/typecheck ограничены
`compileall` (pyright/ruff в окружении отсутствуют — см. §8).

## 4. Цепочка `sync → public → verify → edit → archive → restore`

Воспроизведена дважды: (а) прогоном `test_admin_e2e` (2 теста, два WSGI-сервера по
настоящему HTTP на loopback) и (б) вручную на staging (§3 `docs/DEPLOYMENT.md`), где
всё видно глазами:

| Шаг | Проверка | Результат |
|---|---|---|
| sync | `python3 -m tests.dev_release … --db …` (реальный ночной цикл на offline-фикстурах) | `published: true`, `release_id: rel_f05ac2e006667619`, все 4 источника `ok` |
| public | `GET /money?period=2025-07`, `/api/public/money`, `/api/public/export/categories.csv` | `200`, суммы в копейках + `amount_rub`, CSV из allowlist |
| verify | `checks.verify(period=2025-07)` из стола | `effective_state == verified` |
| edit | `news.edit` (правка летописи новой ручной версией) | `manual_state == edited`, ревизия v1 в `news_revisions` |
| archive | `close_period` + `correction` (новая версия с diff и причиной) | `report_snapshots` = v1, v2; v1 не перезаписан |
| restore | `ops.cli backup` → `ops.cli restore` на **чистом** каталоге | `usable: true`, integrity `ok`, v1/v2, курсоры, участники, 17 транзакций — на месте; восстановленный экземпляр отдаёт `/reports/2025-07` с текстом правки |

Дополнительно проверено, что копия «живого» WAL-файла — не бэкап
(`test_a_file_copy_of_a_live_wal_database_is_not_a_valid_backup`): у живого файла
есть `-wal`, а API-бэкап даёт `integrity ok` и полные строки.

## 5. Защита HTML / CSV / JSON и замер времени, RSS, диска

* **HTML:** экранирование (`html.escape(quote=True)`) применяется ко всем
  недоверенным строкам; проверено на `<script>`, `<img onerror>`, разрыве
  атрибута. Публичные страницы не имеют JS вообще (CSP `default-src 'none'`).
* **JSON:** каждый ответ API проходит `assert_no_leaks` (запретные ключи + id- и
  e-mail-паттерны значений); тело ошибки guard не проходит (§2.2), но содержит
  только эхо запроса вызывающего.
* **CSV:** фиксированный `CSV_HEADER`, только категории целых месяцев/года; добавлена
  нейтрализация формул (§2.1). `transaction_id`/`account_id`/контрагентов нет.
* **RSS/sitemap:** продукт их не публикует — падающих на них утечек нет; тест
  `test_rss_sitemap_and_feed_surfaces_do_not_exist` фиксирует отсутствие
  `/rss.xml`, `/feed.xml`, `/feed`, `/atom.xml`, `/sitemap.xml` (404), чтобы
  незащищённая лента не появилась молча. Если лента понадобится — только через тот
  же `assert_no_leaks`.
* **Время:** планировщик задан явно в `Europe/Moscow`;
  `systemd-analyze calendar` подтверждает нормализацию; `is_due`/`next_run_at`
  не зависят от часового пояса хоста (проверено в MSK, UTC и CET); логический день
  — календарный день МСК; публичный `check` допускает на страницах единственное
  время — публичное «03:00».
* **Диск:** БД 336 КБ (84 страницы × 4096, WAL), релиз 32 КБ, дерево staging
  1.3 МБ, репозиторий 8.4 МБ (`.git` 3.7 МБ); на диске свободно 17 ГБ из 29 ГБ.
  Бэкап — один самодостаточный файл + манифест.

## 6. Скриншоты

29 файлов в `docs/screenshots/`. Публичная часть (1–16) — прошлой карточки.
Редакционный стол (17–29) снят **в этой карточке** на staging по loopback через
реальный Chromium/CDP. Скриншоты стола в открытый репозиторий не включаются:
на них построчные операции и настройки; файлы остаются только в закрытом контуре.

`17-desk-overview-desktop`, `18-desk-money-desktop`, `19-desk-events-desktop`,
`20-desk-news-desktop`, `21-desk-reconciliation-desktop`,
`22-desk-publish-desktop`, `23-desk-settings-desktop`, `24-desk-audit-desktop`,
`25-desk-channel-desktop`, `26-desk-publish-period-desktop`,
`27-desk-publish-group-desktop` (групповой период `2025-01..2025-12` — тот самый
§2.3), `28-desk-login-desktop` (страница входа), `29-desk-overview-mobile-390`
(360/390: `scrollWidth == clientWidth == 390`, горизонтальной прокрутки нет).

Сессия стола для снятия выдана штатным `sessions.create_session` в staging-БД
(подписанного Telegram-OIDC входа нет — он в списке доступов §8); это прямо
оговорено: перед нами отрендеренные реальным приложением страницы по реальной
сессии, а не макет.

## 7. Staging: что и как поднималось

* Публичный сайт — `127.0.0.1:8099` (`randreport.web serve`), релиз из
  синтетического ночного цикла, БД в scratch-каталоге.
* Стол — `127.0.0.1:8098` (`randreport.admin serve`), секрет подписи cookie —
  локальный staging-литерал, `RANREPORT_INSECURE_COOKIES=1` только для plain-HTTP
  loopback.
* HTTPS-проверка — Caddy 2.6.2 с `tls internal` на `127.0.0.1:8443` (сайт) и
  `127.0.0.1:8444` (стол), изолированный конфиг
  `deploy/caddy-staging-check.Caddyfile`, системный `/etc/caddy/Caddyfile`
  (чужая служба `185.121.113.*.sslip.io`) не читался на запись и не менялся.
* Всё, что не loopback, не поднималось; ни один внешний хост не опрашивался;
  платные сервисы не подключались; `NEWS_LLM_PROVIDER=none`.
* Побочный эффект HTTPS-проверки снят: `tls internal` устанавливает локальный CA
  Caddy в системное доверенное хранилище. После проверки CA удалён
  (`sudo rm -f /usr/local/share/ca-certificates/Caddy_Local_Authority_*.crt`;
  `sudo update-ca-certificates --fresh`), `~/.local/share/caddy` удалён — на
  машине не осталось ни сертификата Caddy, ни листенеров staging. Порядок уборки
  записан в `docs/DEPLOYMENT.md` §5 и в самом `deploy/caddy-staging-check.Caddyfile`.
* Проверки по HTTPS (сырые сокеты, чтобы видеть настоящие заголовки):
  `https://127.0.0.1:8443/` → `200`, HTTP/2, сертификат внутреннего CA
  (SAN `IP:127.0.0.1`); `https://127.0.0.1:8444/admin/login` → `303` с
  `Set-Cookie: rd_admin_login=…; Path=/admin; HttpOnly; SameSite=Lax; Max-Age=900; Secure`
  и `Cache-Control: no-store`, `X-Robots-Tag: noindex`,
  `CSP … script-src 'none'`; plain HTTP на TLS-порт → `400`.

Замечание для будущих проверок в этом окружении: обычный `curl`/`urllib` по
loopback проходит через промежуточный слой, который подмешивает свой `Set-Cookie`
и из-за которого значения cookie в выводе маскируются. Заголовки приложения видны
только по сырому сокету (как сделано выше) — иначе легко принять чужую cookie за
свою.

## 8. Пробелы (что осталось непокрытым)

1. **Реальные доступы.** Все источники и вход — синтетические. Настоящие Zenmoney,
   Teamup, Telegram (Bot API + MTProto), Telegram OIDC, домен/хостинг и приватный
   GitHub не подключены. Единый список — `docs/DEPLOYMENT.md` §8.
2. **Telegram OIDC.** Проверяется на синтетическом провайдере и синтетических
   ключах (ES256/RS256 сверены с `openssl`); реальный issuer/JWKS/redirect не
   сверялись.
3. **Ночной прогон на боевом таймере.** Таймер-шаблоны не устанавливались;
   `systemd-analyze calendar` подтверждает выражение, но фактического запуска
   `Persistent=true` после простоя нет.
4. **HTTPS.** Проверено за локальным внутренним CA; боевой ACME-сертификат,
   редирект `http→https` и **HSTS** не проверены (HSTS — действие терминатора,
   `docs/DEPLOYMENT.md` §5).
5. **Внешний URL и роли.** Внешнего URL нет; роли проверены как «все действующие
   коммунары равны», но на одном тестовом участнике.
6. **RSS/sitemap** отсутствуют как продуктовая функция — если нужны, их надо
   реализовать через ту же allowlist-проекцию (§5).
7. **Статический анализ.** `pyright`/`ruff` в окружении отсутствуют; проверка типов
   ограничена `compileall` и диагностикой редактора. Диагностика редактора при
   этом находит в тестах уже существовавшие замечания об Optional-доступах
   (`dict.get(...)` в `assertIn`/`split`); они не влияют на выполнение (все 348
   тестов проходят) и не исправлялись, чтобы не смешивать рефакторинг тестов с
   приёмкой. Это ограничение среды, а не решение.
8. **Инфографика черновика** (опциональная, §4) не реализована.
9. **Правило идентичности донатеров** не восстановлено — публикуются только общие
   показатели и уже публичные имена.
10. **Маскировка cookie промежуточным слоем** (см. §7) может сбить с толку при
    проверке заголовков обычным `curl`; для приёмки использовать сырой сокет.

Ни один из этих пробелов не отменяет зафиксированные результаты: всё, что
помечено как проверенное, проверено на реальном коде и синтетических данных, а
`mock` нигде не выдаётся за `prod`.
