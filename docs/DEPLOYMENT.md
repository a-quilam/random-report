# Размещение, staging, rollout и rollback

Документ отвечает на вопросы «где и как это запускается», «как откатиться», «как
проверяется HTTPS» и «какие доступы ещё реально нужны». Источник требований —
`docs/REQUIREMENTS.md` §9 (надёжность, бэкапы, стоимость), §11 (закрытый стол),
§13 (`O01`: восстановление, `.env.example`, отсутствие секретов, документированные
deployment/rollback). Контракты слоёв — `docs/WEB.md`, `docs/INGEST.md`,
`docs/ADMIN.md`; эксплуатация — `docs/RUNBOOK.md`.

Ничего из этого документа не выполнялось вовне: remote не настроен
(`git remote -v` пуст), платные сервисы не подключались, посторонние службы на
машине не менялись.

## 1. Что именно разворачивается

Один репозиторий, один код, три роли одного процесса. Каждая роль — отдельная
systemd-служба (`deploy/`), с собственным набором секретов и своим доступом к
файлам:

| Роль | Служба | Команда | Слушает | Секреты | Файлы |
|---|---|---|---|---|---|
| Ночной цикл | `randreport-sync.service` + `.timer` | `python -m randreport.ops.cli run` | — (oneshot по таймеру 03:00 МСК) | `ZENMONEY_TOKEN`, `TEAMUP_*`, `TELEGRAM_BOT_TOKEN` | пишет приватную БД и каталог релиза |
| Публичный сайт | `randreport-public.service` | `python -m randreport.web serve --host 127.0.0.1 --port 8099` | loopback → TLS-терминатор | **нет** | читает только каталог релиза |
| Редакционный стол | `randreport-admin.service` | `python -m randreport.admin serve --host 127.0.0.1 --port 8098` | loopback → TLS-терминатор | `RANREPORT_ADMIN_SECRET`, `TELEGRAM_OIDC_*` | пишет приватную БД |
| Бэкап | `randreport-backup.service` + `.timer` | `deploy/randreport-backup.sh` | — (oneshot, 04:00 МСК) | `RANREPORT_BACKUP_PASSPHRASE` | читает приватную БД, пишет `/var/backups/randreport` |

**Инвариант изоляции публичного процесса** (`deploy/randreport-public.service`,
проверяется `tests/test_ops_isolation.py`): у публичной службы нет
`EnvironmentFile`, она не получает ни одного токена источника, а `RANREPORT_DB`
не только не задан, но и **игнорируется** командой `serve` в любом случае.
Страницы `/reports/*` строятся из allowlist-хранилища внутри релиза
(`reports.json`, которое ночной цикл публикует вместе с разделами), поэтому
приватная БД публичному процессу не нужна вообще. Публичный процесс ничего не
пишет: каталог релиза смонтирован только для чтения (`ReadOnlyPaths=`), а
`ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`,
`NoNewPrivileges=true` закрывают остальное.

**Публикация по умолчанию запрещена.** Оба процесса слушают `127.0.0.1`.
Не-loopback адрес возможен только при двойном явном согласии: флаг CLI
`--allow-external-bind` И переменная `RANREPORT_ALLOW_EXTERNAL_BIND=1`
(`randreport/bindguard.py`). Случайная одна переменная в unit-файле не откроет
сайт наружу. TLS-терминатор ставится перед loopback-портом.

Общее состояние — один SQLite-файл (`RANREPORT_DB`) в WAL-режиме; публичная
проекция — каталог релиза с атомарным указателем `current`
(`randreport/ops/publish.py`). Публичный процесс не читает секретов и не ходит к
источникам; стол держится на loopback до появления TLS.

Плановый запуск — `deploy/randreport-sync.timer` + `.service`
(`OnCalendar=*-*-* 03:00:00 Europe/Moscow`, `Persistent=true`), бэкап —
`deploy/randreport-backup.timer` + `.service` (04:00 МСК). Шаблоны не
устанавливались.

## 2. Staging на loopback (сделано в этой карточке)

Синтетические данные, никаких внешних сервисов:

```bash
STAGE=/var/lib/randreport-staging            # в этой проверке — каталог в scratch
mkdir -p "$STAGE/releases"
python3 -m tests.dev_release "$STAGE/releases" --period 2025-07 --db "$STAGE/randreport.sqlite"
python3 -m randreport.web seed --db "$STAGE/randreport.sqlite"
# пересобрать релиз, чтобы reports.json (allowlist-хранилище) попал в каталог
python3 -m tests.dev_release "$STAGE/releases" --period 2025-08 --db "$STAGE/randreport.sqlite"
RANREPORT_DB="$STAGE/randreport.sqlite" python3 -m randreport.admin bootstrap --db "$STAGE/randreport.sqlite" --telegram-id <согласованный-ID>

# публичный сайт: только каталог релиза, приватная БД не подключается
RANREPORT_RELEASE_DIR="$STAGE/releases" \
  python3 -m randreport.web serve --host 127.0.0.1 --port 8099 &
# стол — loopback, со своим секретом
RANREPORT_DB="$STAGE/randreport.sqlite" RANREPORT_ADMIN_SECRET=<локальный-секрет-≥16> \
  RANREPORT_INSECURE_COOKIES=1 python3 -m randreport.admin serve --host 127.0.0.1 --port 8098 &
```

Публичный `serve` намеренно **не** принимает `RANREPORT_DB`: если нужно прогнать
локальный `check` по приватной БД, это отдельная команда `python -m randreport.web check`
(операторский инструмент), а сам сервис читает только релиз. Для staging-проверки
против БД есть явный флаг `--report-db` (или `RANREPORT_REPORT_DB`).

`RANREPORT_INSECURE_COOKIES=1` **допустим только** на локальном staging по
`http://127.0.0.1`: в проде cookie несёт `Secure` (проверено, см. §5).

## 3. Варианты размещения

Требования: только стандартная библиотека приложения, один файл БД, TLS,
редкий ночной запуск, низкая стоимость (§9). Отсюда три варианта.

### A. Один VPS + systemd + Caddy (рекомендуемый)

* процесс-хосты: `randreport-public.service`, `randreport-admin.service` (loopback),
  ночной цикл — `randreport-sync.timer` + `.service`, бэкап —
  `randreport-backup.timer` + `.service` (шаблоны в `deploy/`);
* TLS-терминатор — Caddy с автоматическим ACME; сайт и стол проксируются на
  loopback-порты, стол дополнительно закрыт basic-auth/VPN или оставлен на
  loopback и доступен через SSH-туннель до подключения OIDC;
* бэкап — `backup_database` по таймеру (см. §6) в отдельное место;
* стоимость — один VPS; внешних платных API нет (провайдер новостей —
  опционален, по умолчанию `NEWS_LLM_PROVIDER=none`);
* почему рекомендуется: минимум движущихся частей, файловая БД там же, где
  writer, ночной цикл не требует постоянной нагрузки.

### B. VPS + статический экспорт (read-only публикация)

* публичный сайт не запускается вообще: `python -m randreport.web export --out DIR`
  в ночном цикле, каталог отдаёт Caddy/любой статический сервер;
* стол живёт на том же VPS, доступен через туннель;
* плюс: публичный процесс нельзя скомпрометировать (нет Python на фронте),
  нулевой риск утечки через серверные ошибки; минус: нет `/api/public/*` и
  динамических периодов, период появляется только следующим экспортом.

### C. PaaS/контейнер (не рекомендуется для этого продукта)

* приложение запускаемо в контейнере, но SQLite-файл требует постоянного тома, а
  WSGI-хост обязан быть однопроцессным или с общим томом; бесплатные тарифы PaaS
  часто дают эфемерную ФС — тогда молча теряется история Teamup, что прямо
  запрещено §7;
* выбирать только при явном требовании «без своего сервера» и с внешним
  управляемым диском + отдельным бэкапом.

Отклонено: CDN/edge-рендеринг (страницы строятся из приватной БД), noSQL/внешняя
БД (лишняя стоимость и вторая точка отказа), serverless (нет постоянного диска),
любые платные сервисы до согласования бюджета (§9).

## 4. Rollout

Выкладка кода в `/opt/randreport` идёт **только** через
`deploy/randreport-deploy.sh` (один слот, обязательный перезапуск, манифест,
`web check`, дым и автоматический откат) — см. раздел «Выкладка кода в
`/opt/randreport`: один слот». Ручной `rsync` запрещён: именно он дал
параллельную выкладку двух карточек и `500` на живом сайте.

1. **Проверка перед выкладкой (обязательный шлюз):**
   `python3 -m unittest discover -s tests -t .` (348 тестов OK) и
   `python3 -m randreport.web check` → `problem_count: 0`.
2. Ночной цикл один раз вручную в dry-run на реальных (read-only) источниках:
   `python -m randreport.ops.cli run --dry-run`; сверить `last_good` каждого
   источника, `data_as_of`, покрытие, `issue`-коды.
3. Если новый payload не прошёл проверку приватности/сходимости — публикация не
   переключается (`ops/publish.py` пишет новую ревизию и только потом меняет
   симлинк `current`); прежняя безопасная ревизия остаётся.
4. Включить таймеры: `systemctl enable --now randreport-sync.timer
   randreport-backup.timer`, проверить
   `systemd-analyze calendar '*-*-* 03:00:00 Europe/Moscow'`.
5. Публичный сайт поднять первым (loopback), затем TLS-терминатор, затем стол.
6. Стол — только после согласования OIDC и списка коммунаров; до этого он не
   публикуется и доступен через loopback/туннель.

## 5. Проверка HTTPS (выполнена на loopback)

Терминатор: Caddy 2.6.2 с локальным (internal) CA на 127.0.0.1, без изменений
системного `/etc/caddy/Caddyfile`. Конфигурация проверки (изолированная,
loopback-порты 8443/8444, `admin off`) — `deploy/caddy-staging-check.Caddyfile`.

Результаты (зафиксированы в `docs/TEST_RESULTS_ACCEPTANCE.md`):

* `caddy validate` → `Valid configuration`;
* `https://127.0.0.1:8443/` → `200`, HTTP/2, сертификат внутреннего CA
  (`CN=Caddy Local Authority - ECC Intermediate`, SAN `IP:127.0.0.1`) — то есть
  приложение корректно работает за TLS-терминатором;
* `GET https://127.0.0.1:8444/admin/login` → `303` с
  `Set-Cookie: rd_admin_login=…; Path=/admin; HttpOnly; SameSite=Lax; Max-Age=900; Secure`,
  `Cache-Control: no-store…`, `X-Robots-Tag: noindex…`,
  `Content-Security-Policy … script-src 'none'` — флаг `Secure` выставлен, до
  настоящего TLS стол остаётся на loopback;
* plain HTTP на TLS-порт → `400` (понижения нет);
* публичные ответы несут CSP, `nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY`,
  `ETag`.

**Пробел (действие при выкладке):** `Strict-Transport-Security` приложение не
ставит — HSTS обязан добавлять TLS-терминатор (в Caddy:
`header Strict-Transport-Security "max-age=31536000; includeSubDomains"`), иначе
первый переход по `http://` остаётся возможен. Проверить после подключения
домена: `curl -sI https://<домен>/ | grep -i strict-transport`.

Проверка боевого сертификата после выкладки:

```bash
curl -sSI https://<домен>/ | head -1
echo | openssl s_client -connect <домен>:443 -servername <домен> 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

Инструмент проверки из этой карточки, если нужен повторно:
`deploy/caddy-staging-check.Caddyfile` + `caddy run --config <файл> --adapter caddyfile`
с `XDG_DATA_HOME`/`XDG_CONFIG_HOME` в отдельном каталоге.

**Побочный эффект проверки и его уборка.** `tls internal` заставляет Caddy
установить свой локальный корневой CA в **системное** доверенное хранилище (он
вызывает `update-ca-certificates`, если есть права). После проверки это надо
снять, иначе на машине остаётся посторонний корневой сертификат:

```bash
sudo rm -f /usr/local/share/ca-certificates/Caddy_Local_Authority_*.crt
sudo update-ca-certificates --fresh
rm -rf ~/.local/share/caddy        # если caddy запускался без XDG_DATA_HOME
```

В этой карточке так и сделано: `ls /usr/local/share/ca-certificates/` пуст,
`ls /etc/ssl/certs | grep -i caddy` пуст, `~/.local/share/caddy` удалён,
системный `/etc/caddy/Caddyfile` (чужая служба `185.121.113.*.sslip.io`) не
менялся, листенеров staging не осталось.

## 6. Бэкап, восстановление и измеримость

```bash
# консистентный зашифрованный бэкап (SQLite backup API, не копия файла)
RANREPORT_BACKUP_PASSPHRASE=<секрет-из-store-0600> \
python -m randreport.ops.cli backup --db /var/lib/randreport/randreport.sqlite \
  --out /var/backups/randreport/randreport-$(date -u +%F).sqlite.enc --encrypt
# проверка бэкапа: integrity_check + обязательные таблицы + версия схемы
python -m randreport.ops.cli verify-backup --backup /var/backups/…/randreport-YYYY-MM-DD.sqlite
# восстановление на чистом экземпляре (отказывается перезаписывать существующую БД без --force)
RANREPORT_BACKUP_PASSPHRASE=<секрет> \
python -m randreport.ops.cli restore --backup /var/backups/…/randreport-YYYY-MM-DD.sqlite.enc \
  --db /var/lib/randreport/randreport.sqlite
# retention: 14 дневных + одна месячная; единственную копию не удаляет никогда
python -m randreport.ops.cli prune-backups --out /var/backups/randreport --keep-daily 14 --keep-monthly 1
```

Ежедневный цикл собран в `deploy/randreport-backup.sh`
(`randreport-backup.timer`): он пишет зашифрованную копию, **сразу проверяет
восстановление** во временном файле и удаляет его, затем применяет retention.
Скрипт отказывается писать незашифрованную копию, если
`RANREPORT_BACKUP_PASSPHRASE` не задан.

Шифрование — AES-256-CBC/PBKDF2 через системный `openssl`; пароль передаётся
дочернему процессу **в окружении**, никогда в аргументах (аргументы видны в
`ps`). Формат самодостаточен: зашифрованный файл имеет суффикс `.enc`, и
восстановление без пароля отклоняется, а не пишет мусор поверх БД.

Бэкап — самодостаточный один файл (после API-копии БД переводится в
`journal_mode=DELETE`, пустые `-wal`/`-shm` не остаются), рядом пишется
`*.manifest.json` (с полями `encrypted` и `cipher`). Историю Teamup и курсоры
источников нельзя терять: retention выбирается так, чтобы всегда оставалась копия
старше самого старого локального события (§7). Практично: 14 суток дневных копий
+ одна месячная; `prune_backups` гарантирует, что если копия одна, она
не удаляется (`tests/test_ops_isolation.py`, `reason=single_copy`).

Замеры (staging, синтетика): БД ≈ 336 КБ (84 страницы × 4096), релизные файлы —
32 КБ, дерево проверки целиком — 1.3 МБ, приложение+тесты+документация — 8.4 МБ
без `.git`. Свободно на диске в окружении: 17 ГБ из 29 ГБ. Стоимость: внешних
платных вызовов нет; единственный потенциально платный элемент — провайдер
новостей, по умолчанию выключен (`NEWS_LLM_PROVIDER=none`).

## 7. Rollback

Откат — всегда через **новую** ревизию, никогда через перезапись (инвариант R01):
закрытая версия отчёта неизменяема, исправление приходит новой версией с diff и
причиной.

* **Публичная проекция (проверяемый откат):** указатель `current` возвращается
  на **уже опубликованную** ревизию, целевую ревизию перед этим загружает
  allowlist-валидатор, а после переключения она читается ещё раз:

  ```bash
  python -m randreport.ops.cli rollback --publish-dir /var/www/randreport --to rel_…  --dry-run   # только проверить
  python -m randreport.ops.cli rollback --publish-dir /var/www/randreport --previous            # на предыдущую
  ```

  Вывод содержит `verified: true/false`, `previous` и список разделов; команда
  возвращает 0 только если указатель действительно переехал и релиз снова
  читается (`randreport/ops/publish.py:rollback_release`). Откат на отсутствующий,
  испорченный или проваливший allowlist каталог **отклоняется**, а не публикуется.
  «Предыдущая» ревизия берётся из порядка публикации (mtime каталогов релиза), а
  не из имени: id релиза — это хэш содержимого, порядка в нём нет. Если цель —
  снять раздел/релиз при подозрении на утечку, не откатываться на небезопасный
  снимок (`docs/RUNBOOK.md`).
* **Стол:** «откатить» сопоставление можно только новой версией
  (`/admin/actions/mapping/rollback`), суммы — никогда в столе (только в Zenmoney).
* **БД:** восстановление из последнего проверенного бэкапа (`ops.cli restore`);
  после восстановления `python -m randreport.web check` должен дать 0 проблем.
* **Код:** штатный откат — `sudo deploy/randreport-deploy.sh rollback` (архив
  `/var/backups/randreport/opt-randreport-pre-deploy-*.tgz`: распаковка, перезапуск
  служб, дым); история — `git revert` без миграций-разрушителей: миграции
  только добавляют таблицы (`migrations.py`), поэтому старая ревизия кода
  работает на новой схеме; откат схемы не требуется.

## 8. Единый список реально оставшихся доступов

Всё ниже — единственное, что не закрыто кодом. Ничего из этого не выдавалось и не
подключалось; секреты в чат не запрашивались.

| # | Доступ | Зачем | Вид | Блокирует |
|---|---|---|---|---|
| 1 | Zenmoney: отдельный уже настроенный аккаунт, read-only токен | ночной сбор финансов (§6) | секрет в secret store | реальный сбор, прод |
| 2 | Teamup: calendar key + token, read-only | события и история (§7) | секрет | реальный сбор, прод |
| 3 | Telegram Bot API token + имя публичного канала | суточный снимок подписчиков (§8), только чтение | секрет | раздел «Сообщество» |
| 4 | Telegram MTProto-сессия (read-only, заданный публичный канал) | полная история постов с дневным pull (§8); альтернатива — webhook-inbox | сессия в secret store + авторизация владельца | история «Летописи» |
| 5 | Telegram OIDC: issuer / client_id / redirect_uri + согласованный список действующих коммунаров | вход в закрытый стол (§11) | конфиг + решение владельца | доступ редакции, прод стола |
| 6 | Telegram Login widget: bot token | вход по классическому виджету (§11) | секрет | резервный вход |
| 7 | Домен + хостинг (VPS) + DNS | размещение и HTTPS (§9) | решение владельца | прод целиком |
| 8 | Публичный репозиторий GitHub `a-quilam/random-report`: право push через deploy key | публикация кода (снапшот-поток, `sync-public-snapshot.py`) | доступ | публикация кода |
| 9 | Решение по правилу идентичности донатеров | публикация имён/ранжирования (§12) | решение владельца | публичный блок донатеров |
| 10 | Опционально: провайдер LLM для новостей | извлечение новостей (§8) | секрет, **платный** — только по согласованию | качество «Летописи» (без него — non-LLM путь) |
| 11 | Опционально: инфографика черновика (SVG/PNG) | оформление поста | дизайн | не блокирует |

Все секреты — только через локальный secret store сервисного пользователя
(EnvironmentFile с правами `0600`), никогда через чат, аргументы команд или git
(`.env.example` содержит только `placeholder-*`; проверено тестом
`SecretHygieneTests`). Доступы 1–4 — строго read-only по коду клиентов
(`randreport/connectors/*`): запись в источники невозможна.

## Приём публичных постов без личного аккаунта (§8)

Webhook — приём событий, а не опрос: он лишь пишет входящие обновления в приватный
inbox, публикация происходит в 03:00. Служба запускается отдельно от ночного
таймера и не требует внешней авторизации:

```
TELEGRAM_WEBHOOK_SECRET='<16+ символов>' TELEGRAM_PUBLIC_CHANNEL=@… \
  python -m randreport.ops.cli webhook --db /var/lib/randreport/randreport.sqlite --port 8081
```

`setWebhook` из кода не вызывается: регистрация адреса в Telegram — действие
владельца вне репозитория (наружный прокси/TLS — за пределами задачи). Пропущенный
период восстанавливается импортом публичного экспорта:

```
python -m randreport.ops.cli import-news --db … --file export.json [--channel @…]
```

В `status` видны `telegram_posts` (приём+commit) и `inbox_counts`
(pending/committed). Пока нет согласованной MTProto-сессии истории, раздел
«Летопись» публикуется с `completeness=partial` и явными причинами в payload
источника — пустой inbox не выдаётся за «нечего публиковать».

## 9. Что установлено в бою (t_1b5b08c8)

Проверено на текущем VPS без собственного домена. Пользователь `randreport`
(system, nologin); код — копия рабочего дерева в `/opt/randreport`
(root:randreport, `0750`, без `.git` и кэшей), интерпретатор —
`/opt/randreport/.venv` (venv **без сторонних пакетов** — приложение живёт на
стандартной библиотеке, поэтому venv нужен только чтобы шаблоны служб
устанавливались дословно).

Разграничение файлов:

| Путь | Владелец:группа | Права | Назначение |
|---|---|---|---|
| `/opt/randreport` | root:randreport | `0750` | код (службе только чтение) |
| `/etc/randreport` | root:randreport | `0755` | конфигурация (в каталоге нет секретов) |
| ``<закрытый каталог приватных карт>`/` | root:randreport | `0750` | карты контура/календаря и решения владельца |
| `/etc/randreport/{randreport,admin,backup}.env` | root:randreport | `0600` | секреты источников, секрет стола, пароль бэкапа |
| `/var/lib/randreport` | randreport | `0700` | приватная БД, lease, состояние опроса |
| `/var/www/randreport` | randreport | `0755` | публичный каталог релизов |
| `/var/backups/randreport` | randreport | `0700` | шифрованные копии |

Службы и автозапуск — §1. Публичный TLS-терминатор — **отдельный** экземпляр
Caddy (`deploy/randreport-caddy.service`, `deploy/Caddyfile`): слушает `:80/:443`
с `CAP_NET_BIND_SERVICE` (не root), имеет своё состояние в `/var/lib/caddy` и не
использует и не включает системную службу `caddy`. Адрес —
`rand.185.121.13.113.sslip.io` (sslip.io для публичного IP), сертификат Let's
Encrypt по ACME; HSTS добавляет терминатор.

Приём новых постов без домена — `deploy/randreport-getupdates.{service,timer}`
(каждые 10 минут): `getUpdates` хранит обновления не дольше 24 часов, поэтому
опрос частый. Перед опросом читается `getWebhookInfo`; при зарегистрированном
webhook опрос **отказывается** работать, а `setWebhook`/`deleteWebhook`/
`drop_pending_updates` не вызываются — чужой webhook и очередь не сбрасываются.
Durable offset (`telegram_poll_state`) продвигается только после записи строк
inbox. Бэкап — §6; разбор найденных при запуске дефектов, доказательства
публичного и локального контуров — `docs/TEST_RESULTS_LAUNCH.md`.

## 10. Что выпущено с утверждёнными правилами (t_6a308cd3)

Тот же VPS и тот же адрес `https://rand.185.121.13.113.sslip.io`, без нового домена.

* **Код.** Рабочее дерево `main` (слияние `revision/visual` + `revision/allocation`
  плюс правка вёрстки) скопировано в `/opt/randreport` (root:randreport, `0750`, без
  `.git` и кэшей); кэш байткода вычищен. Службы `randreport-public` и
  `randreport-admin` перезапущены, `randreport-caddy` не трогался.
* **Карта контура.** Приватный файл `<закрытый каталог приватных карт>/finance-policy.json`
  заменён картой с утверждёнными классами, публичными именами, подсказками и
  правилами распределения (прежний файл рядом с суффиксом `.bak-…`, `0640`
  root:randreport). Та же карта записана в приватный store новой версией
  (`owner-categories-1`), поэтому стол, `web build` и диагностика читают ровно тот
  же подтверждённый контур. Миграция 7 (`finance_report_exclusions`) применена.
* **Релизы.** Сбор выполнен в отдельный каталог, проверен (`web check` — 312
  страниц, 0 проблем) и только затем опубликован штатной службой
  `randreport-sync.service`. Каталог `/var/www/randreport` содержит 38 каталогов
  релизов (по числу периодов), `current` — `rel_5722d903e92a6e71`.
* **Резерв и откат.** Шифрованная копия приватной БД с проверкой восстановления
  (`integrity: ok`, `usable: true`), архив каталога релизов и архив копии кода — в
  `/var/backups/randreport/`. Откат релиза — `python -m randreport.ops.cli rollback
  --previous` (или `--to <id>`); откат версии карты — `mapping/rollback`; вернуть
  файл карты и копию кода — из названных архивов.
* **Таймер.** На время выкладки `randreport-sync.timer` был остановлен и включён
  заново; ближайший запуск — 03:00 Europe/Moscow (`systemctl list-timers`).
* **Аренда.** Согласованные 90 000 ₽/мес действуют с `2026-11` (`rent.from_month`);
  месяцы раньше сохраняют фактический платёж, правило в прошлое не распространяется.
* Приёмка, сверка старое/новое по месяцам и году, найденный дефект вёрстки и границы
  — `docs/TEST_RESULTS_RELEASE.md`.

## Выкладка кода в `/opt/randreport`: один слот

Наблюдение 2026-11-24 (выпуск `t_5cd0d9f8`): две релизные карточки писали в
`/opt/randreport` одновременно. Код заменился под работающим процессом, служба не
перезапустилась, живой адрес отдавал `500` на всех маршрутах (включая `/static/*`),
а `.venv` службы был снесён — после этого `randreport-public.service` не запускался
(`203/EXEC`). Тесты такого класса дефектов не видят: процесс держит старые модули в
памяти, а шаблоны и статика читаются с диска.

Поэтому выкладка кода — **одна дверь**: `deploy/randreport-deploy.sh`. Ручной `rsync`
в `/opt/randreport` запрещён; вторая выкладка ждёт слота, а не перезаписывает дерево.

```bash
cd /home/agent/projects/random-report
# выкладка main: слот, копирование, перезапуск служб, проверки, дым, отчёт
sudo RANREPORT_DEPLOY_REPORT=/var/lib/randreport-deploy/last-report.txt \
  deploy/randreport-deploy.sh deploy
# проверки «манифест + web check + дым» одной командой, ничего не меняя
sudo deploy/randreport-deploy.sh verify
# ручной откат из архива (по умолчанию — последний снятый)
sudo deploy/randreport-deploy.sh rollback
# выполнить произвольную команду под слотом выкладки
sudo deploy/randreport-deploy.sh slot true
```

| Требование | Как выполнено | Код возврата |
|---|---|---|
| один слот для всех релизных карточек | `flock` на `/var/lock/randreport-deploy.lock`; вторая выкладка ждёт (`flock -n` в цикле, heartbeat каждые 30 с) | `3` — не дождалась за `RANREPORT_DEPLOY_SLOT_WAIT` (900 с); дерево не тронуто |
| источник — ровно `main` | `git archive` коммита `main` (другой ref — отказ без `RANREPORT_DEPLOY_ALLOW_REF=1`), незакоммиченные правки отслеживаемых файлов — отказ | `2` |
| `--delete` и неприкосновенный `.venv` | `rsync -a --delete --exclude=.venv` из распакованного коммита | `2`/`4` |
| владелец и режимы | `root:randreport`, каталоги `0750`, файлы `0640`, git-режим `100755` → `0750` | `4` (аудит режимов) |
| обязательный перезапуск | `systemctl restart randreport-public randreport-admin` сразу после копирования; по каждой службе `is-active`, `ExecMainStatus=0`, `Result=success`; если активна `randreport-sync.service` (идёт сбор) — сначала ожидание | `4` |
| проверяемость одной командой | `verify`: `sha256sum -c` манифеста (пути из git, хэши из коммита) + `randreport.web check` (0 проблем) + дым (те же проверки выполняются и внутри `deploy`) | `1` |
| дым до объявления успеха | `https://rand.185.121.13.113.sslip.io/` и `/static/approved-home.css` → `200`; для `/static/*` ещё и sha256 отдачи живого адреса сверяется с выложенным файлом (200 сам по себе не доказывает, что сайт отдаёт выпущенный код) | `4` |
| неудача не оставляет `500` | автоматический откат из `/var/backups/randreport/opt-randreport-pre-deploy-*.tgz`, перезапуск, повторный дым и явная строка «сайт ОТКАЧЕН» в отчёте | `4` (откачено) / `5` (откат не удался — нужен человек) |

Резерв снимается **до** копирования (всё дерево вместе с `.venv`): именно `.venv` был
потерян при параллельной выкладке. В `/var/backups/randreport/` хранятся последние
`RANREPORT_DEPLOY_BACKUP_KEEP=10` архивов выкладки; ночной `prune-backups` их не
трогает — он фильтрует по `*.sqlite.enc`.

Отчёт выкладки печатается целиком (и дублируется в `RANREPORT_DEPLOY_REPORT`, если
он задан): слот, коммит, архив, число файлов, результат каждой проверки, коды дыма и
итоговая строка — она же идёт в отчёт релизной карточки. Аргументы и переменные
окружения перечислены в шапке скрипта (`deploy/randreport-deploy.sh --help`).

Разбор дефекта, прогоны и доказательства — `docs/TEST_RESULTS_DEPLOY_SLOT.md`.

## Публикация кода в GitHub (политика 28.11.2026)

Официальный репозиторий проекта — **публичный** `a-quilam/random-report`;
владелец хочет открытый код для сообщества. Приватное зеркало не создаётся.

* Поток публикации — снапшот-синхронизация (`sync-public-snapshot.py` в закрытом
  каталоге доступа): из рабочего репозитория публикуются отслеживаемые файлы
  `randreport/`, `tests/`, `docs/`, `deploy/`, `scripts/`, `config/`,
  `.gitignore`, `.env.example` (README и CHANGELOG — публичные файлы самого
  репозитория). Локальная git-история (коммиты с внутренними артефактами) в
  публичный репозиторий не переносится — у публичного репозитория своя история
  снапшотов, push только fast-forward.
* Закрытый контур не публикуется: скриншоты стола (`docs/screenshots/*desk-*`,
  на них построчные операции и настройки), `__pycache__`, бинарные
  базы/сессии/ключи. Публичные скриншоты сайта (агрегаты) — публикуются.
* Перед каждым push — обязательный секрет-скан по значениям и формам
  (токены источников, ключи, ID, персональные данные) по рабочему дереву и
  по всем blob'ам опубликованной истории; сканер ведёт список заведомо
  публичных значений (названия категорий с живого сайта, пути по умолчанию).
* Секреты, конфигурации с ключами, закрытые финансовые операции, внутренние
  документы коммуны и персональные данные публикуются только в агрегатах,
  уже разрешённых публичной проекцией.
* Тесты проходят до коммита и до push; push проверяется чтением `ls-remote`
  и свежим клоном.
