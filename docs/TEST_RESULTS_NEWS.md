# Результаты тестов: транспорт новостей Telegram без личного аккаунта (§8)

Задача `t_ae20e13d`, ветка `rel/channel`, baseline `b3c25da`. Контур: приём
публичных `channel_post`/`edited_channel_post` webhook-ом с проверкой секрета и
разрешённого канала, лимит тела, дедуп и durable commit, ночное чтение inbox,
импорт пропущенного публичного JSON, конфигурируемый LLM-адаптер с бюджетом 0 по
умолчанию и честным gap вместо новостей без провайдера. Разделение снимка
подписчиков и истории/новостей. Контракт — `docs/INGEST.md` §§2.3, 2.4, 5, 7, 8.

Только синтетика: придуманные посты, loopback-серверы (`wsgiref`), пустой JSON-
экспорт Telegram Desktop, ключи-заглушки. Реальных постов, сессий, токенов и
платных запросов нет; `setWebhook` не вызывается, исходящих запросов к Telegram
нет.

## Команды

```
cd /home/agent/projects/random-report-channel
python3 -m unittest discover -s tests -t .                      # весь suite
python3.14 -W error::ResourceWarning -m unittest discover -s tests -t .
python3 -m compileall -q randreport tests
python3 -m randreport.web build --periods 2025-07,2025
python3 -m randreport.web check
```

## Фактический прогон

```
Ran 418 tests in 23.729s
OK
resource guard: no unclosed sqlite connection, listening socket or http response
```

418 = 365 прежних (`b3c25da`) + 53 новых в `tests/test_n03_news_transport.py`.
Оба интерпретатора: `python3` (3.13.5) — 418 OK; `python3.14` (3.14.9) — 418 OK;
оба с `-W error::ResourceWarning` и стражем ресурсов (0 утечек).
`python3 -m compileall -q randreport tests` — чисто.
`python3 -m randreport.web build --periods 2025-07,2025` → два релиза;
`python3 -m randreport.web check` → 120 страниц, 0 проблем.

| Класс | Тестов | Покрывает |
|---|---|---|
| `TestWebhookIntake` | 17 | секрет (401, отказ старта без секрета), allowlist (403, пустой allowlist), лимит тела (413), дедуп повторной доставки, правка как новая версия, повтор старой версии, чужой тип апдейта, подпись-подпись, 400 на битый JSON/апдейт, 429 + `Retry-After`, путь/метод, отсутствие `setWebhook`/`getUpdates` в исходниках |
| `TestWebhookToNightly` | 9 | HTTP→inbox→nightly→архив, идемпотентность ночного чтения, правка в архиве, лимит commit 200 + `news_inbox_backlog`, пустой inbox = gap (не `complete`), inbox не объявляет полноту, полный проход при рабочей истории и провайдере, провайдер с бюджетом 0 не тратит денег, partial-inbox не ломает другие разделы |
| `TestImportMissedPublicJson` | 7 | импорт экспорта, повтор без дублей, нечитаемые записи в отчёте, форма Telegram Desktop с `date_unixtime`, отказ на не-экспорт, деньги не тронуты + запись `news_imports`/audit, импорт доходит до публичной летописи |
| `TestLlmAdapter` | 10 | конфиг по умолчанию (бюджет 0, provider none), выбранный провайдер без бюджета тоже выключен, отказ на неполный конфиг, реальный HTTP-вызов на loopback, 429 → defer без повторов, таймаут → ограниченные ретраи, кэш против второго платного вызова, суточный бюджет, нулевой лимит токенов не блокирует положительный лимит вызовов, типизированные ошибки и отсутствие ключа в `repr` |
| `TestSnapshotApartFromHistory` | 4 | снимок пишется при недоступной истории, ошибка снимка не становится ошибкой новостей, ошибка истории не становится ошибкой снимка, `status` показывает inbox и не печатает секрет |
| `TestInboxUnit` | 4 | валидация записи inbox, commit помечает все pending-строки сообщения, разбор «не наш» апдейт, экспорт через пакет |
| `TestWebhookCommand` | 2 | CLI отказывается стартовать без секрета (rc 2, секрет не печатается), сборка приложения из окружения и allowlist |

## Ручной сквозной smoke через реальные CLI (вне suite)

`webhook` (loopback) → файловая БД → `python -m randreport.ops.cli run` →
публикация: webhook ответил `accepted`, затем `revised`; `run --dry-run` → rc 0,
`news` и `telegram_posts` со `status=ok` и `coverage=partial`, симлинк `current`
не переключён; `run` → `published=true`, `chronicle.completeness=partial`,
`news_count=0` (провайдера нет — честный gap); `import-news --file export.json` →
`{"imported": 2, "rejected": 1, "reasons": ["entry 2: message id 'x' is not numeric"]}`;
`status` → `telegram_posts`/`news` `ok/partial` и `inbox_counts`
(`pending_messages: 0`, `messages: 1`). Секретов в выводе нет.

## Границы проверки

Проверены код и синтетика. Не выполнено и не ожидалось: реальный приём от
Telegram (наружный прокси/TLS, регистрация адреса человеком вне репозитория),
согласованная MTProto-сессия истории (`MTProtoHistorySource.is_placeholder = True`,
поэтому покрытие новостей остаётся `partial`), платный LLM-провайдер (бюджет 0 по
умолчанию), systemd-служба webhook-а и размещение. Публичные данные макета не
выкладывались.
