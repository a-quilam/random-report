# Проверки: топ-3 пересылок Telegram через MTProto (t_ca904198)

Дата: 28.11.2026. Ветка `web/mtproto-forwards` (worktree
`/home/agent/projects/random-forward-stats`). Полное ТЗ — карточка
t_ca904198; решение владельца от 28.11.2026: Telethon 1.46.x с bot-логином,
TGStat и альтернативы запрещены.

## 1. Что изменилось

| Компонент | Изменение |
|---|---|
| `randreport/store/migrations.py` | миграция 8: `public_message_archive.media_album_id` + таблица `post_forward_stats` (`forwards`, `views`, `fetched_at`, `channel_id`; `NULL` = не измерялось) |
| `randreport/connectors/telethon_stats.py` | **новый** — Telethon-транспорт: секреты только из окружения, батчи ≤100 id, FloodWait без ретраев, Telethon импортируется лениво |
| `randreport/store/repository.py` | `media_album_id` в `upsert_posts`/`archived_posts`; `upsert_post_counters`, `post_counter_rows`, `latest_counter_fetch`, `announcement_news_items` |
| `randreport/projection/forwards.py` | **новый** — окно 30 дней по Москве, отбор анонсов, схлопывание медиагрупп, сортировка, `AnnouncementsDTO` |
| `randreport/domain/dto.py` | `ForwardItemDTO`, `AnnouncementsDTO` (строгие allowlist-DTO) |
| `randreport/ops/pipeline.py` | секция `announcements` в каждом релизе (периодонезависимое окно) |
| `randreport/web/release_index.py` | чтение секции `announcements` (старые релизы без неё — `None`) |
| `randreport/ops/run.py` | источник `telegram_stats`: измеряет всё окно заново каждый проход |
| `randreport/ops/cli.py` | адаптер в ночной цикл; ручной проход `forwards`; `telegram_stats` в `status` |
| `randreport/web/approved_home.py`, `home_approved.html` | блок «ЧТО ПЕРЕСЫЛАЛИ»: подзаголовок с точными датами окна, три честных состояния, род слова «пересылки» |
| `randreport/web/app.py` | главная читает `release.announcements` |
| `docs/INGEST.md`, `docs/WEB.md` | контракты |

## 2. Обязательные сценарии §9 — таблица

| # | Сценарий | Проверка |
|---|---|---|
| 1 | Реальное получение счётчиков | **не выполнялось** — секретов MTProto нет в машине (см. §5); транспорт проверен контрактными тестами + сверка с проверенным владельцем вызовом (`get_messages(ids)` → `forwards`/`views`) |
| 2 | Обновление счётчиков загруженных постов | `test_rerun_updates_values_without_duplicating_rows` |
| 3 | Исключение постов старше 30 дней | `test_posts_outside_the_window_do_not_rank_even_when_measured` |
| 4 | Граница периода по Москве | `test_window_is_thirty_inclusive_moscow_days`, `test_moscow_boundary_decides_membership` (20:30 UTC = 29/30.10 23:30 MSK) |
| 5 | Исключение не-анонсов | `test_only_announcements_rank_and_the_rest_is_excluded`, скрытые/удалённые — `test_hidden_or_deleted_announcements_do_not_rank` |
| 6 | Сортировка по пересылкам | `test_sort_is_forwards_desc_then_fresher_then_id` |
| 7 | Ровно три при достатке | `test_at_most_three_cards` |
| 8 | Одна-две карточки | `test_one_and_two_items_pass_through` |
| 9 | Медиагруппа без дублирования | `test_a_media_group_is_one_publication_with_the_main_message_counter`, `test_two_announcements_of_one_album_collapse_into_one` |
| 10 | Сохранение при ошибках Telegram | `test_unanswered_posts_keep_their_previous_values`, `test_telegram_unavailable_raises_and_changes_nothing` |
| 11 | Ноль ≠ неизвестное | `test_unknown_forwards_excluded_measured_zero_included`, `test_a_measured_zero_is_a_zero_and_unknown_stays_null` |
| 12 | Ссылки на оригиналы | `test_links_point_at_the_original_posts`, `test_unsafe_url_never_becomes_a_card` |
| 13 | Повторный запуск без дублей | `test_rerun_updates_values_without_duplicating_rows` + миграция/чтение секции — `test_release_index_reads_the_section_back`, `test_release_without_the_section_keeps_none` |
| 14 | Остальные разделы работают | suite 849 OK; `web check` 312 страниц 0 проблем на релизах из копии боевой БД; preview-обход всех страниц → 200 |
| 15 | Секретов нет в Git и ответах | `test_no_secrets_in_the_public_payload`, `test_payload_counts_only_no_values_and_no_secrets`, `ConfigTests` (имена переменных без значений, `repr` пуст); grep по каталогу релиза — чисто |

## 3. Прогоны

- `python3.13 -W error::ResourceWarning -m unittest discover -s tests -t .` → **849 OK** (807 прежних + 42 новых в `tests/test_forward_stats.py`), leakguard чистый.
- `python3.14.9 -W error::ResourceWarning` → **849 OK**.
- `compileall` чисто; `web check` на синтетике — 120/0, на копии боевой БД — 312/0.
- Два теста обновлены под новые факты: `test_e02_opaque_revision` (миграция 8 применяется к schema-3 хранилищу) и setup `test_ops_deploy_slot` (песочница выкладки сажается на `origin/main` — клон worktree-ветки раньше оставался на её ветке и ломал 11 сценариев, сама логика скрипта не менялась).
- Лик-гард `assert_no_leaks` сам отклонил поле `message_id` в публичном DTO (`forbidden_key`) — поле убрано из DTO, ссылка несёт id в URL.

## 4. Развёртывание preview

- Релизы собраны кодом ветки из копии боевой БД (SQLite backup API, только чтение): миграция 7→8 применилась идемпотентно, 38 релизов.
- `/var/www/randreport-preview` (0755/0644, randreport:randreport); `randreport-preview.service` переключён на код ветки + bind-mount каталога preview-релизов вместо `/var/www/randreport` (боевой выпуск не тронут).
- Проверено по HTTPS: `/`, `/money`, `/events`, `/community`, `/support`, `/chronicle`, `/reports`, `/methodology`, `/reports/2026-11`, `/api/public/overview`, `/healthz` → все 200; блок печатает «ТОП-3 АНОНСА · 30.10—28.11.2026» и честное состояние.

## 5. Честные ограничения

1. **Интеграционная проверка с реальными счётчиками НЕ выполнялась**: учётных данных MTProto в машине нет (`<закрытый каталог доступа>/source-secrets.json` их не содержит, в `/etc/randreport/*.env` нет). Владелец вносит значения командой `python3 <закрытый каталог доступа>/enter-mtproto-tokens.py` → `mtproto-secrets.json` (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_STATS_BOT_TOKEN`, `TELEGRAM_CHANNEL_USERNAME`). Код читает те же имена из окружения процесса; для боевой службы значения добавляются в `/etc/randreport/randreport.env`. После этого: ночной цикл 03:00 МСК измеряет окно сам, вручную — `python -m randreport.ops.cli forwards`. Непроверенную реализацию за рабочую не выдаём.
2. **Анонсов в хранилище пока ноль**: `news_items` в боевой БД пуст (LLM-классификатор выключен, бюджет 0). Рейтинг читает уже сохранённые `classification='announcement'` и сам ничего не классифицирует: даже после подключения секретов блок останется в честном состоянии «анонсов с измеренными пересылками нет», пока анонсы не появятся в `news_items` (включение LLM-прохода или ручная разметка столом). Реальных постов в 30-дневном окне архива: 8.
3. Боевой выпуск (прод) и ночной цикл прод-кода не трогались — это отдельная карточка после приёмки; формат релиза обратно совместим (старый читатель игнорирует лишнюю секцию).
