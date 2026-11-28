# Боевой запуск: что установлено, чем проверено, что осталось владельцу

Карточка `t_1b5b08c8`. Документ фиксирует **фактический** постоянный запуск на
текущем VPS (без собственного домена) и доказательства по каждому пункту
разрешения владельца. Публичный и локальный контуры показаны раздельно.
Секретов, идентификаторов счетов/каналов, названий категорий и сумм здесь нет.

Разрешение владельца (комментарий к карточке, 2026-11-22 18:46) и что из него
сделано:

| Требование владельца | Состояние |
|---|---|
| Постоянный запуск на текущем VPS, без домена | установлены 6 служб, автозапуск включён |
| Данные первого админа и режим `contains` — в закрытом `production-owner.json` | файл перенесён в ``<закрытый каталог приватных карт>`/` (0600), режим применён |
| Не раскрывать отдельные неизвестные операции/неутверждённые суммы | денежный раздел публикует **две** обезличенные строки (приход/расход), без разбивки по категориям |
| Установить и проверить службы, разграничение файлов, автозапуск/таймер, first admin | сделано, см. §1–§3 |
| Публичный доступ без домена: проверяемый безопасный адрес | `https://rand.185.121.13.113.sslip.io` — сертификат Let's Encrypt, HSTS |
| Не открывать admin/сырьё | стол на loopback, снаружи `/admin/` → 404, Терминатор проксирует только проекцию |
| Приём постов через `getUpdates`, а не обязательный webhook | `ops.cli getupdates` + таймер каждые 10 минут; webhook не регистрируется и не сбрасывается (§5) |
| Проверить matcher на подстроке внутри слитного названия | режим `contains` теперь подстрочный; на реальных данных это одна дополнительная дата (§6) |
| LLM: только публичные посты, бюджет 0 | провайдер `none`, `llm_budget_ledger` пуст (§7) |
| Доказательства службы и доступности, раздельно контуры | §3 (локальный), §4 (публичный) |

## 1. Что установлено

Пользователь `randreport` (system, nologin). Код — копия рабочего дерева в
`/opt/randreport` (root:randreport, `0750`, без `.git`/кэшей), интерпретатор —
`/opt/randreport/.venv` (только стандартная библиотека, сторонних пакетов нет).

Разграничение файлов (`ls -ld`):

```
drwxr-x---  root randreport  /opt/randreport            код, службе только чтение
drwxr-xr-x  root randreport  /etc/randreport            конфигурация служб
drwxr-x---  root randreport  <закрытый каталог приватных карт>/   приватные карты (0750)
-rw-------  root randreport  /etc/randreport/randreport.env   секреты источников (0600)
-rw-------  root randreport  /etc/randreport/admin.env        секрет стола (0600)
-rw-------  root randreport  /etc/randreport/backup.env       пароль бэкапа (0600)
-rw-r-----  root randreport  <закрытый каталог приватных карт>/finance-policy.json    карта контура
-rw-r-----  root randreport  <закрытый каталог приватных карт>/calendar-mapping.json  карта календаря
drwx------  randreport       /var/lib/randreport        приватная БД и state
drwxr-xr-x  randreport       /var/www/randreport        публичный каталог релизов
drwx------  randreport       /var/backups/randreport    шифрованные копии
```

Службы (шаблоны в `deploy/` устанавливаются в `/etc/systemd/system/` без правок):

| Служба | Тип | Что делает |
|---|---|---|
| `randreport-sync.service` + `.timer` | oneshot, 03:00 Europe/Moscow | ночной сбор и публикация; единственный писатель приватной БД и каталога релиза |
| `randreport-getupdates.service` + `.timer` | oneshot, каждые 10 минут | приём новых постов канала через `getUpdates` (§5) |
| `randreport-backup.service` + `.timer` | oneshot, 04:00 Europe/Moscow | шифрованная копия + немедленная проверка восстановления + retention |
| `randreport-public.service` | simple, автозапуск | публичный сайт на `127.0.0.1:8099`, только релиз, без секретов |
| `randreport-admin.service` | simple, автозапуск | редакционный стол на `127.0.0.1:8098` |
| `randreport-caddy.service` | simple, автозапуск | отдельный экземпляр Caddy: TLS-терминатор **только** проекции |

Автозапуск: `systemctl is-enabled randreport-public.service randreport-admin.service
randreport-caddy.service randreport-sync.timer randreport-backup.timer
randreport-getupdates.timer` → `enabled` (у `public`/`admin` добавлен раздел
`[Install]`, иначе `enable` их не включал). Календарные выражения проверены
`systemd-analyze calendar`: `03:00 Europe/Moscow` → ближайшее срабатывание
**01:00 CET**, `04:00` → **02:00 CET**, опрос `*-*-* *:00/10:00`.

## 2. Реальный ночной цикл

Запуск **штатной службы** (не ручной командой), реальные источники:

```
systemctl start randreport-sync.service
Result=success  ExecMainStatus=0  Consumed 4.4s CPU, 32.3M memory peak
```

Результат в журнале: `"published": true`, 38 релизов по периодам (2024-01 … 2026-11
+ годовые), `contour_confirmed: true`, `policy_version: rules-v4`,
источники `zenmoney ok/complete`, `teamup ok/complete`, `telegram ok/complete`,
`telegram_posts ok/partial`, `news ok/partial`, `issues: []`. Таблицы БД:
459 календарных записей, 3774 движения, схема 6 (миграция getUpdates-offset
применена на живой БД). Повторные прогоны дают тот же безопасный результат.

`python -m randreport.web check --release-dir /var/www/randreport` →
**312 страниц, 0 проблем**.

## 3. Локальный (закрытый) контур

* публичный процесс: `http://127.0.0.1:8099/` → `200`; служба не читает
  приватную БД (`RANREPORT_DB` командой `serve` игнорируется, проверяется
  `tests/test_ops_isolation.py`);
* стол: `GET /admin` без сессии → `303 → /admin/login`; подделанные cookie
  (`rrd_session`, `rd_admin_session`, …) → `303`; `/admin/login` → `200`;
  OIDC-провайдер не настроен и страница входа честно это сообщает;
* прослушивание только на loopback: `ss -tlnp` → `127.0.0.1:8098`, `127.0.0.1:8099`;
* внешний bind без двойного отказа отклоняется: `bindguard.assert_bind_allowed("0.0.0.0")`
  → отказ с текстом про `--allow-external-bind` И `RANREPORT_ALLOW_EXTERNAL_BIND=1`.

### 3.1 Роли (first admin)

Первый администратор внесён в производственную БД:
`python -m randreport.admin bootstrap --db /var/lib/randreport/randreport.sqlite
--telegram-id <согласованный ID>` → `{"bootstrapped": true, "members": 1}`.

Вход в стол включён через Telegram Login widget штатным bot token (`admin.env`,
дополнительного секрета не заведено; OIDC можно подключить позже отдельной
правкой). Проверка на живом столе (сырые сокеты: слой окружения переписывает
cookie-заголовки):

| Сценарий | Результат |
|---|---|
| подпись виджета для согласованного ID | `303` + выдан session-cookie |
| `GET /admin` с этой сессией | `200` (роль работает) |
| подпись виджета для постороннего ID | `403`, cookie не выдана |
| испорченная подпись | `401` |
| устаревшая подпись | `401` |
| без сессии | `303` |

## 4. Публичный контур (внешний адрес)

`https://rand.185.121.13.113.sslip.io` — sslip.io-имя для публичного IP этого VPS:
собственный домен не требуется, но это обычный сертификат Let's Encrypt по ACME
(HTTP-01 на порту 80; challenge обслужили 7 адресов Let's Encrypt).

```
HTTP/2 200
strict-transport-security: max-age=31536000
content-security-policy: default-src 'none'; style-src 'self'; …
X-Frame-Options: DENY, X-Content-Type-Options: nosniff
subject=CN=rand.185.121.13.113.sslip.io issuer=C=US, O=Let's Encrypt, CN=YE1
notBefore=Nov 22 2026  notAfter=Feb 20 2027
http://… → 308 Permanent Redirect → https://…
```

Доступность разделов снаружи (все `200`): `/`, `/money`, `/events`,
`/community`, `/chronicle`, `/support`, `/methodology`, `/reports`,
`/reports/2026-09`, `/api/public/{overview,money,events,community,chronicle}`,
`/api/public/export/categories.csv`, `/robots.txt`, `/healthz`.

Админка наружу не выходит: `GET https://…/admin/` → `404` (терминатор не
проксирует стол). Приватная БД и сырьё наружу не публикуются.

Публичная проекция — строгий allowlist: скан опубликованного каталога не находит
ни одного ID счета/подкалендаря, ни сырых названий, ни разбивки по категориям
(§8).

## 5. Приём постов через `getUpdates` (без webhook)

Добавлен `randreport/inbox/polling.py` и действие `ops.cli getupdates`
(таймер каждые 10 минут; `getUpdates` хранит обновления не дольше 24 часов, поэтому
опрос частый). Правила карточки выполнены буквально:

* сначала читается `getWebhookInfo`; пока **любой** webhook зарегистрирован, опрос
  **отказывается** работать. `setWebhook`/`deleteWebhook`/`drop_pending_updates`
  не вызываются нигде — чужой webhook и очередь не сбрасываются;
* в один вызов запрашиваются только `channel_post`/`edited_channel_post`;
* в inbox пишутся только посты разрешённого канала (чужой чат — `foreign`,
  непостовое обновление — `ignored`, битое — `rejected`), дедуп по `update_id`;
* durable offset (`telegram_poll_state`, миграция 6) продвигается **после** записи
  строк, поэтому сбой между двумя записями перечитывает то же и дедуплицирует.

Проверка на живом боте: `getMe` → `ok`, `getWebhookInfo` → `url` пуст,
`pending_update_count` менялось с 1 до 0 после опроса; вывод действия —
`{"polled": true, "webhook": {"url_set": false, …}, "offset": …}`, строка offset
в БД создана таймером (`Result=success`), а не ручным прогоном.

## 6. Matcher на слитном названии

Режим `contains` (записанное правило владельца `source_title_contains_word`,
`case_sensitive: false`) теперь проверяет **подстроку**, а не слово с границами:
слово внутри слитного названия тоже считается собранием. На реальных данных это
ровно одна дополнительная дата: в периоде `2026-09` опубликовано 6 собраний
вместо 5, добавлена дата, которую слитное название содержит. Режим `exact`
остаётся консервативным по умолчанию; обработка неизвестного режима —
отказ. Тесты: `tests/test_meeting_word.py`,
`tests/test_getupdates_polling.py`, `tests/test_calendar_meetings.py`.

## 7. LLM и бюджет

`NEWS_LLM_PROVIDER=none` в `randreport.env`; `llm_budget_ledger` — 0 строк,
`extraction_cache` — 0 строк. Платных вызовов не было. «Летопись» публикуется с
явным пробелом (`completeness: partial`), пустой inbox не выдаётся за полное
покрытие. LLM подключается только на публичных постах канала и только после
выделенного ключа и жёсткого лимита.

## 8. Приватность публикации

* **Неизвестные суммы не раскрываются.** Денежный раздел публикует две
  обезличенные строки (`key=k_unclassified`, label «требуют классификации»,
  приход и расход) — ровно те два итога, что уже видит страница; разбивка по
  исходным категориям в вывод не попадает (осталась в приватной БД и столе).
  `completeness: partial`, `coverage_is_final: false`,
  `status_labels: unclassified_present, subsidy_unknown, …`.
* **Скан утечек** по всему опубликованному каталогу: 0 совпадений ID счетов и
  подкалендарей, 0 сырых названий, 0 хеш-ключей категорий.
* **Устаревшие ревизии не остаются на диске.** Пересборка периода после правки
  кода/карты создавала новый каталог релиза и оставляла прежний; он тоже проходил
  allowlist, и сайт мог отдать устаревшую (более подробную) проекцию. Теперь
  публикация удаляет вытесненную ревизию того же периода
  (`ops.publish.prune_superseded_releases`), а выбор релиза для периода
  детерминирован (`ReleaseIndex.by_period`: текущая ревизия, иначе новейшая).
  Итог: 38 каталогов, 38 периодов, дубликатов нет, во всех релизах по 2 строки
  денежного раздела.

## 9. Бэкап и восстановление

Штатный блок `randreport-backup.service` (реальный прогон):

```
{"backup": "/var/backups/randreport/randreport-2026-11-22.sqlite.enc",
 "cipher": "aes-256-cbc/pbkdf2", "encrypted": true, "integrity": "ok",
 "schema_version": 6, "row_counts": {...}}
{"expected_schema_version": 6, "integrity": "ok", "missing_tables": [],
 "path": "/tmp/randreport-restore-check.…", "schema_version": 6, "usable": true}
{"deleted": [], "kept": ["…"], "reason": "single_copy"}
```

То есть копия шифруется, восстановление проверяется сразу во временном файле и
удаляется, retention единственную копию не удаляет. Пароль — в
`/etc/randreport/backup.env` (0600).

## 10. Найденные и закрытые при запуске дефекты

1. ``<закрытый каталог приватных карт>`/` был `0700`, файлы `0600` — ночная служба не могла
   прочитать карту контура и падала `Permission denied`. Каталог `0750`, карты
   `0640` (группа `randreport`).
2. Шаблоны `randreport-public/admin.service` не имели `[Install]`, поэтому
   `enable` не включал автозапуск. Добавлен `WantedBy=multi-user.target`.
3. Вытесненные ревизии релиза оставались в публичном каталоге и могли быть
   отданы (утечка более подробной проекции) — добавлены
   `prune_superseded_releases` и детерминированный `by_period`.
4. Денежный раздел публиковал по строке на каждую неизвестную категорию
   (15 строк с суммами) — теперь одна обезличенная строка на направление.
5. Гигиена: ручная команда с секретами в аргументах попала в journald; записи
   удалены (`journalctl --rotate && --vacuum-time=1s`). Впредь секреты не
   передаются через командную строку.

## 11. Что осталось владельцу (внешние входы)

* **OIDC** (`TELEGRAM_OIDC_ISSUER/CLIENT_ID/REDIRECT_URI`) — если нужен вход
  через Telegram OIDC вместо виджета; стол уже работает на виджете.
* **Классификация категорий по устойчивым ID** — до неё денежный раздел остаётся
  честно неполным (одна обезличенная строка); карта `C01–C31` у владельца.
* **Решение по неопознанному** — сегодня агрегат; если нужно иначе, это
  отдельная правка.
* **Правило идентичности донатеров** — публичный блок донатеров по-прежнему не
  публикуется.
* **RSS/sitemap** — функции нет (зафиксировано, не реализовано).
* **Офсайт-копия бэкапа** — сейчас копия и пароль на том же VPS.

## 12. Как проверить и откатить

```bash
# состояние служб и таймеров
systemctl status randreport-public admin caddy; systemctl list-timers 'randreport-*'
# публичная доступность и заголовки
curl -sSI https://rand.185.121.13.113.sslip.io/ | head -1
# ночной цикл руками (штатной службой)
systemctl start randreport-sync.service
# откат публичной проекции на опубликованную ревизию (никогда не перезаписью)
python -m randreport.ops.cli rollback --publish-dir /var/www/randreport --previous
# откат кода
git revert <commit>   # миграции только добавляющие, откат схемы не нужен
```
