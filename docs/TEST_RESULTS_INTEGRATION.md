# Интеграция четырёх веток и сквозная приёмка релиз-кандидата

Карточка: `t_60dd112b` — «Объединить изменения и принять релиз целиком через
браузер и nightly». Входы (родители): `t_2cf0c4d8` (собрания в main),
`t_399e116c` (`rel/finance`), `t_ae20e13d` (`rel/channel`), `t_14543ca6`
(`rel/ops`), `t_c2ea6720` (`rel/calendar-adapter`, дополнительная зависимость по
комментарию к карточке). Ожидающие дети: `t_a831193f` (доведение staging и
безопасный GitHub-релиз), `t_3eeb34dc` (`web build` без `RANREPORT_DB`),
`t_1b5b08c8` (боевой запуск).

Проверялись **только код и синтетические данные**: фиктивные HTTP-API на
loopback, синтетические приватное хранилище, карты и релизы. Платные сервисы не
подключались, реальные источники не опрашивались, приватные вложения и секреты не
открывались, remote не настраивался, ничего не выкладывалось.

## 1. Объединение

Исходный SHA был устойчив, все ветки чисты; базовая точка — `b3c25da`.

| Ветка | Коммит | База |
|---|---|---|
| main (собрания) | `e9e0397` | `b3c25da` |
| `rel/finance` | `f0b0d8e` | `b3c25da` |
| `rel/channel` | `d497580` | `b3c25da` |
| `rel/ops` | `8419e05` | `b3c25da` |
| `rel/calendar-adapter` | `0f4856b` | `b3c25da` |

Ветки слиты в `main` по графу (merge-коммиты `ab29e24`, `c8f7e28`, `9290d06`,
`202e52c`). Конфликты разрешены **реальной логикой**, не механически:

* **`randreport/ops/cli.py`** (конфликтовал со всеми четырьмя ветками). Побеждает
  более новая логика ночного цикла: карта контура — общий загрузчик
  `projection/policy.py` (`_policy_for`), маппинг календаря — из БД
  (`event_mapping_from_store`), публикация — по нескольким периодам
  (`build_releases`). Зашитая «пустая» политика (`env-policy-unset`), которую
  приносили `rel/channel` и `rel/ops`, отброшена: это тот самый дефект, что
  исправила `rel/finance`. Дополнения веток сохранены полностью: набор действий
  CLI (`import-news`, `webhook`, `prune-backups`, `rollback`), опции обеих веток,
  редакция логов (`redaction.redact`), чтение `--policy`.
* **`randreport/web/build.py`** — побеждает вызов общего `build_releases`; при
  этом из `rel/ops` сохранён `release_files`, и он перенесён в общий путь
  публикации (`ops/pipeline.build_releases`), иначе ночной цикл публиковал бы
  релизы без `reports.json`, и раздел `/reports/*` у боевых релизов ломался.
* **Карта календаря** — загрузчики переехали из `web/build.py` в
  `projection/policy.py` вместе с `rel/finance`, поэтому в них заново вписан
  meeting-контур из main (`excluded/meeting/rent subcalendars`, `meeting_titles`,
  `meeting_label`) и ограничение `KNOWN_EVENT_TYPES` исходными типами
  (`SOURCE_TYPES`), а не всеми значениями `EventType`.
* **`randreport/store/migrations.py`** — обе ветки называли свою миграцию «4»:
  ревизия (TEXT) осталась 4, inbox перенумерован в 5; тест апгрейда схемы 3
  теперь ждёт `(4, 5)`.
* **`randreport/domain/events.py`, `docs/DOMAIN.md`, `docs/INGEST.md`,
  `docs/PROGRESS.md`, `.env.example`** — union обеих сторон, решения
  перенумерованы по порядку.

## 2. Найденные и закрытые пробелы

| # | Что | Почему это реальный пробел | Где закрыт |
|---|---|---|---|
| 1 | Загрузчик маппинга календаря после слияния не знал meeting/rent/excluded | решения владельца по собраниям и закрытым арендам молча терялись бы в ночном цикле и веб-сборке | `randreport/projection/policy.py` |
| 2 | `reports.json` публиковался только веб-сборкой `rel/ops`, но не общим путём ночного цикла | боевой релиз шёл бы без архива отчётов, `/reports/*` зависел бы от приватной БД | `randreport/ops/pipeline.py` |
| 3 | Документированные `TEAMUP_BASE_URL` и `TELEGRAM_BASE_URL` игнорировались `build_adapters` | коннекторы нельзя направить на staging/loopback, документированная настройка не действовала | `randreport/ops/cli.py` (`.env.example`) |
| 4 | Публикуемый архив отчётов раскрывал автора правки (`latest.corrections[].actor`) | §3.7/§11 и A01: публичная выдача не раскрывает, кто правил | `randreport/web/reports.py` (`_public_snapshot`) |
| 5 | Полоса покрытия не работала под CSP самого сайта (`style-src 'self'` блокирует атрибут `style`) | браузерная проверка показала: ширины сегментов отбрасывались, полоса раскладывалась по тексту подписей — график вводил в заблуждение, на 390px подписи обрезались | `randreport/web/render.py` (SVG-геометрия) + `randreport/web/static/site.css` |

## 3. Команды и результаты

```
$ python3 -m compileall -q randreport tests
(без вывода)
$ python3 -W error::ResourceWarning -m unittest discover -s tests -t .
Ran 568 tests in 45.2s
OK
resource guard: no unclosed sqlite connection, listening socket or http response
$ python3.14 -W error::ResourceWarning -m unittest discover -s tests -t .
Ran 568 tests in 41.4s
OK
$ python3 -m randreport.web build --db … --release-dir … --periods 2025-07,2025   # синтетика
{"published": [["2025-07", "rel_434ef0f112a6f214"], ["2025", "rel_129333103fbc2701"]]}
$ python3 -m randreport.web check --release-dir … --db …
{"pages_checked": 120, "problems": [], "problem_count": 0}
```

Приватный процесс не участвует в публичной выдаче: `serve` поднят с
`RANREPORT_DB=/nonexistent/definitely-not-here.sqlite`, все восемь разделов
ответили `200`, `/reports/*` отрисованы из `reports.json` внутри релиза.

Сканирование секретов по отслеживаемым файлам: единственное совпадение —
синтетическая строка в `tests/test_ops_isolation.py`, доказывающая редакцию
URL с токеном; в `.env.example` только placeholder-значения; в опубликованном
каталоге релиза — 0 вхождений `acc_/occ_/cat_/sub_/cal_/pair_/tx_` и ни одного
секрета.

## 4. Сквозная цепочка (synthetic, реальные CLI)

`tests/test_integration_release.py` (18 тестов) ведёт объединённое дерево целиком:

1. **bootstrap** — мигрированное приватное хранилище, подтверждённая карта
   контура, карта календаря (собрания/аренда/исключение) и одна доставка в inbox;
2. **sync** — настоящий ночной CLI (`python -m randreport.ops.cli run`) тянет
   Zenmoney, Teamup и Telegram через loopback-API; статусы `zenmoney/teamup/
   telegram = ok`, `telegram_posts.coverage = partial` (пустой inbox не выдаётся за
   «новостей нет»), `contour_confirmed = true`;
3. **чтение восьми разделов** — `/`, `/money`, `/events`, `/community`,
   `/support`, `/chronicle`, `/reports`, `/methodology` и JSON-API — без приватной
   БД;
4. **CSV** — `/api/public/export/categories.csv` только категории и доли, без
   идентификаторов;
5. **исправление** — `record_correction` даёт видимую версию 2 с diff и причиной,
   автор остаётся во внутреннем аудите;
6. **сверка** — месячная сумма расходов сходится (`530000` коп.), `web check`
   проходит по всем страницам без проблем;
7. **архив** — `reports.json` публикуется в релиз, `/reports/<период>` рендерится
   публичным процессом без БД;
8. **резервная копия и чистое восстановление** — шифрованный бэкап
   (AES-256-CBC/PBKDF2) восстанавливается на чистый путь, число строк совпадает,
   восстановление без пароля отклоняется.

Подтверждение по собраниям и закрытым событиям (K5 и двойной счёт):

* запись разрешённого контура с точным названием «Собрания» публикуется как
  `{"on": "2025-07-19", "label": "Собрание"}` — и ни одного другого поля;
* более длинное название («Собрания по бюджету») собранием не становится —
  точное совпадение, а не подстрока;
* исключённый подкалендарь (`sub_excluded`, запись с «K5») не даёт ни списков, ни
  итогов: строки `K5`/`sub_excluded`/`ev_excluded` в публичной выдаче нет;
* `total_events == сумма по типам`, `closed_aggregate == closed + closed_rent`,
  `meeting_aggregate == 1` — двойного счёта нет;
* публичные файлы не содержат сырых названий, дат/часов закрытых событий и
  внутренних ID.

## 5. Браузерная проверка (desktop / mobile / клавиатура)

Публичный сайт поднят локально на `127.0.0.1:8099` **без** приватной БД.

* **desktop 1280×900** — все восемь разделов и `10` адресов (включая
  `/reports/2025-03`, `/reports/2025-01..2025-12`) отрисованы, 200, содержимое и
  таблицы на месте; в меню 8 ссылок, период сохраняется в URL.
* **mobile 390×844** — ни одна из восьми страниц не даёт горизонтального
  переполнения (`scrollWidth == innerWidth`), таблицы прокручиваются в своём
  контейнере, легенда полосы переносится.
* **клавиатура** — Tab идёт по порядку (skip-link → логотип → 8 разделов →
  `select` периода → кнопка «Показать» → ссылки содержимого), у каждого
  сфокусированного элемента видимая обводка `3px`; Enter на ссылке «Деньги»
  переводит на `/money?period=2025-07`.
* **CSP** — на всех восьми страницах `0` атрибутов `style` и `0` нарушений
  `securitypolicyviolation`; до правки полоса покрытия под CSP была неверной.
* **Скриншоты** desktop/mobile сняты в ходе проверки (вьюпорты 1280×900 и
  390×844, синтетические данные): они использованы как доказательство этого
  прогона и приватных данных не содержат.

## 6. Матрица требований §13

| ID | Проверка | Где | Итог |
|---|---|---|---|
| F01 | Формулы 60 %/shortfall/результаты | `tests/test_f01_f02_formulas.py` | OK |
| F02 | 110 %, E=0, E<0, null-субсидия, подтверждённый ноль | `tests/test_f01_f02_formulas.py` | OK |
| F03 | Взнос ≠ компенсация, нет удвоения, смешанная категория | `tests/test_f03_f04_classification.py` | OK |
| F04 | Переводы, комиссия, возврат, заём, корректировка, плановая | `tests/test_f03_f04_classification.py` | OK |
| F05 | Новая категория, годовые/месячные суммы сходятся | `tests/test_f05_reconciliation.py` | OK |
| F06 | Копеечная точность, weighted-год, границы января/декабря | `tests/test_f06_precision.py` | OK |
| F07 | Баланс из ликвидных публичных счетов, обязательства отдельно | `tests/test_f07_balance.py` | OK |
| F08 | Карта контура (БД → файл → unconfirmed), реальная схема Zenmoney | `tests/test_f08_nightly_policy.py` | OK |
| S01 | Full/diff/повтор/edit/delete, курсор после commit | `tests/test_s01_zenmoney_sync.py` | OK |
| S02 | Ошибка/partial/restart, last-good сохраняется | `tests/test_s02_resilience.py` | OK |
| S03 | 03:00 Europe/Moscow, catch-up, idempotency, изоляция источников | `tests/test_s03_scheduler.py` | OK |
| E01 | Future/ongoing/end==cutoff, ночное событие, МСК-граница | `tests/test_e01_completed_events.py` | OK |
| E02 | Повторяющиеся, несколько календарей, перенос, удаление, retention, opaque-ревизия | `tests/test_e02_occurrences.py`, `test_e02_teamup_sync.py`, `test_e02_opaque_revision.py` | OK |
| E03 | Закрытое только агрегатом; собрания — дата+константа; исключённый не считается | `tests/test_calendar_meetings.py`, `test_e03_r02_privacy.py`, `tests/test_integration_release.py` | OK |
| T01 | Подписчики: точки/пропуск, ноль не при ошибке | `tests/test_t01_telegram.py` | OK |
| N01 | Новость vs анонс, source refs, инъекция/таймаут/стоимость | `tests/test_n01_n02_news.py` | OK |
| N02 | Изменение/удаление поста, повторный запуск не плодит | `tests/test_n01_n02_news.py` | OK |
| N03 | Webhook-inbox: dedup, commit-before-200, честный gap, бюджет LLM | `tests/test_n03_news_transport.py` | OK |
| H01 | Исторический range, неоднозначный год, конфликты, неполный 2025 | `tests/test_h01_admin_history.py`, `test_history_archive.py` | OK |
| A01 | Вход/nonce/state/aud/expiry, spoof, отзыв, CSRF/IDOR/XSS, no leak | `tests/test_a01_admin_auth.py` | OK |
| A02 | Проверенность по ревизии, инвалидция, issue не стирается | `tests/test_a02_admin_verification.py` | OK |
| R01 | Архив версий, корректировка с diff, исходник не перезаписан | `tests/test_r01_admin_publication.py`, `tests/test_integration_release.py` | OK |
| R02 | Публичный CSV только категории, без личных полей | `tests/test_e03_r02_privacy.py` | OK |
| U01 | Desktop/mobile 390/768/1280, клавиатура, состояния, без логина | `tests/test_web_public_site.py`, браузерная проверка §5 | OK (мобильные вьюпорты проверялись браузером) |
| O01 | Восстановление из бэкапа, миграция на чистой БД, `.env.example`, секретов нет, deployment/rollback | `tests/test_store_migrations.py`, `test_acceptance_independent.py`, `test_ops_isolation.py`, §3 | OK |

## 7. Границы реальной проверки

Проверено **только на синтетике**. Не проверено и остаётся за владельцем:

* живые Zenmoney / Teamup / Telegram (нет согласованных доступов; `MTProto`
  по-прежнему `is_placeholder`), реального трафика источников не было;
* systemd: файлы ролей в `deploy/` не устанавливались, `systemd-analyze calendar`
  и боевой таймер 03:00 Europe/Moscow не запускались (нужен сервер и root);
* публикация во внешний мир: HTTPS/ACME, наружный прокси и внешний URL не
  включались и не проверялись;
* платный LLM-провайдер не вызывался (бюджет 0 по умолчанию);
* GitHub-релиз и любой push не выполнялись — это отдельная карточка `t_a831193f`;
* засечённый ранее дефект `web build` без `--db`/`RANREPORT_DB` (файл `None`)
  остаётся открытой карточкой `t_3eeb34dc`.

## 8. Релиз-кандидат

Точный SHA релиз-кандидата — вершина `main` после этого отчёта (см. поле
`commit` в метаданных задачи). Все ветки вошли в неё; повторная проверка
выполняется командами §3.
