# Приёмка: закрытый редакционный стол (A01, A02, R01, H01, E2E)

Исполняемые доказательства. Команда (из корня репозитория):

```
python3 -m unittest discover -s tests -t .            # вся матрица
python3 -m unittest tests.test_a01_admin_auth tests.test_a02_admin_verification \
                   tests.test_r01_admin_publication tests.test_h01_admin_history tests.test_admin_e2e
python3 -m compileall -q randreport tests
```

Результат на момент коммита (синтетические данные, сеть не используется):

| Набор | Тестов | Результат |
|---|---:|---|
| `tests/test_a01_admin_auth.py` | 38 | OK |
| `tests/test_a02_admin_verification.py` | 20 | OK |
| `tests/test_r01_admin_publication.py` | 16 | OK |
| `tests/test_h01_admin_history.py` | 17 | OK |
| `tests/test_admin_e2e.py` | 2 | OK (по реальному HTTP на loopback) |
| **вся матрица** `discover` | **323** | **OK** (было 230 до этой карточки) |

Статические проверки: `python3 -m compileall -q randreport tests` — ok.
`pyright`/`ruff` в окружении отсутствуют, поэтому статическая проверка
ограничена компиляцией и диагностикой редактора (ограничение то же, что у
соседних карточек).

## Независимая сверка криптографии (вне unittest)

Подписи ES256 и RS256 проверены сторонним инструментом, а не только своим
верификатором: синтетическая пара ключей → подпись → DER/SPKI → PEM →
`openssl dgst -sha256 -verify`.

```
es256 verify (own): True
es256 verify (openssl): Verified OK
es256 tampered (openssl): Verification failure
rs256 verify (own): True
rs256 verify (openssl): Verified OK
```

Скрипт-зонд остаётся в рабочей области разработки (не в репозитории): он
доказывает, что верификатор и «подписывающая» сторона не ошибаются одинаково.

## Матрица приёмки

### A01 — вход, сессии, CSRF, IDOR, XSS, отсутствие утечек

| Критерий `docs/REQUIREMENTS.md` §13 | Тест | Проверено |
|---|---|---|
| подпись/`nonce`/`state`/`aud`/`expiry` | `JoseTests`, `OidcFlowTests` | ES256 и RS256 проходят; `alg: none`, HS256, испорченная подпись, переписанный payload, чужой `iss`, чужой `aud`, истёкший токен, несовпавший `nonce`, пустой `sub` — отклонены; короткий RSA и неизвестный тип ключа — отклонены |
| `state`/PKCE | `test_state_mismatch...`, `test_pkce_challenge_is_s256...` | чужой `state` → отказ; `code_challenge_method=S256`; `code_verifier` доходит до token endpoint и совпадает с challenge; в scope только `openid` |
| неизвестный `kid` | `test_unknown_key_id_refreshes...` | ровно одно обновление JWKS, затем успех |
| HTTPS и redirect allowlist | `test_insecure_urls...` | http-issuer и http-redirect отклонены; redirect вне allowlist отклонён; loopback разрешается только явным флагом |
| неподтверждённый Telegram ID | `test_username_spoof...`, `test_revoked_member_cannot_log_in_again` | подписанный аккаунт вне списка → 403, сессии нет, запись `denied`; username не даёт доступа |
| отозванная сессия | `test_revocation_kills_the_session_immediately`, `SessionAndCsrfTests` | удаление участника немедленно отзывает сессии (`revoked_reason=removed`) и следующий запрос → `/admin/login`; срок и ротация проверены отдельно |
| CSRF | `test_csrf_token_is_required_and_origin_is_checked` | без токена → 403, чужой токен → 403, чужой `Origin` → 403, в аудите `admin.csrf.rejected` |
| IDOR | `test_unknown_and_private_entities_are_not_found`, `test_out_of_contour_operations_are_never_listed` | `tx_private`/`tx_mixed`/несуществующие → 404; в списке операций их нет |
| XSS | `test_stored_markup_is_escaped_everywhere`, `test_notice_reflection_is_escaped` | `<script>`, `<img onerror>`, `"onmouseover` сохраняются и выводятся экранированными; отражение `notice` тоже |
| JSON/export «no leak» | `test_export_carries_no_private_identifiers_or_markup`, `test_error_pages_carry_no_traceback_or_token` | экспорт Markdown без `tx_/acc_/cat_/occ_/pair_`, без HTML; страницы ошибок без трассировки и секретов |
| cookies и заголовки | `test_login_grants_a_hardened_session_cookie`, `test_desk_pages_have_no_javascript...` | `HttpOnly; SameSite=Lax; Secure`; в базе только `sha256` cookie; CSP `script-src 'none'`; `Cache-Control: no-store`; `X-Robots-Tag: noindex`; ни одного `<script>`/inline-обработчика |
| ограничение частоты | `test_login_is_rate_limited` | после N неудач — 429 |
| денежные поля только для чтения | `test_money_fields_are_read_only` | `POST money/edit` → 409, сумма в базе не изменилась; аннотация разрешена и аудируется |

### A02 — равный доступ, проверенность по ревизии, открытые расхождения

| Критерий §4, §13 | Тест | Проверено |
|---|---|---|
| все разрешённые коммунары могут проверять | `test_every_active_member_can_verify_and_change_mapping`, `test_all_members_are_listed_with_equal_standing` | второй участник проверяет строку и меняет сопоставление; в настройках нет роли |
| инвалидация только затронутого | `test_verification_is_bound_to_the_revision` (аннотация не сбрасывает проверку, изменение суммы — сбрасывает), `test_mapping_change_invalidates_finance_and_period_but_not_the_channel`, `test_event_mapping_change_touches_events_only`, `test_news_edit_invalidates_only_that_item`, `test_methodology_change_resets_period_checks_only` | сбросы адресные; журнал `check_invalidations` содержит причину и число затронутых |
| ручная проверка не устраняет расхождение | `test_manual_verification_does_not_close_a_discrepancy`, `test_an_issue_closes_only_when_its_condition_disappears` | проверка при открытой проблеме → состояние `conflict`, проблема остаётся `open`; «известно» не решает; закрывается только когда условие исчезает (и это аудируется) |
| нет данных ≠ оперативные | `test_missing_entity_is_no_data`, `test_summary_counts_only_effective_states` | несуществующая сущность → `no_data`; после изменения данных `verified` не показывается, а считается `stale` |

### R01 — версии, неизменяемость, черновик поста

| Критерий §3.7, §13 | Тест | Проверено |
|---|---|---|
| неизменяемый снимок | `test_closing_freezes_version_one_from_the_live_plane`, `test_a_frozen_version_is_never_rewritten` | закрытие пишет v1 с провенансом, автором и временем; перезапись v1 другим содержимым отклоняется, идентичная — no-op |
| корректировка с diff и причиной | `test_correction_adds_a_version_with_a_diff_and_keeps_the_original`, `test_a_correction_needs_a_reason...` | появляется v2 с `diff` и `reason`, автор фиксируется, v1 не изменён; без причины/поля/изменения — отказ |
| исходный отчёт не перезаписан | `test_correction_invalidates_the_period_check...` | обе версии читаются и проходят `assert_text_faithful`; отметка `report_period` сбрасывается корректировкой |
| текст не придумывает факты | `test_draft_text_states_only_numbers_the_version_contains` | добавленное в текст число → `ReportError` |
| две плоскости не пересекаются | `PlaneSeparationTests` | период с обоими агрегатами берёт одну плоскость; суммы и события не складываются; отсутствие данных честно («нет данных»), прототипные числа не проходят |
| готовность и статусы | `DraftReadinessTests` | `ready` невозможен при блокираторах (непроверенный период, открытые проблемы, неизвестные суммы), возможен после их снятия; неизвестный статус — отказ |
| экспорт | `test_markdown_export_carries_no_private_identifiers` | Markdown содержит пять разделов и не содержит `tx_/acc_/cat_/occ_/pair_/` или HTML |
| публичный слой | `PublicWiringTests` | публичная страница показывает v1/v2 и причину корректировки, v1 доступна по постоянному адресу, закрытие не блокирует живую выдачу (тело API до и после совпадает) |

### H01 — историческая плоскость

| Критерий §12, §13 | Тест | Проверено |
|---|---|---|
| год не есть сумма месяцев | `test_annual_is_not_the_sum_of_the_published_months` | годовая публикация = 459; сумма опубликованных месяцев за январь–март = 151, за январь–август = 298 — год не подменяется |
| групповой отчёт не делится | `test_the_group_report_is_not_split_into_months` | 2025-04..08 = 147 одним периодом; 2025-07 → «частичное покрытие» с указанием группового отчёта; разрыв 2025-02..11 → нет данных |
| неполный 2025 остаётся частичным | `test_the_missing_months_of_2025_are_named_not_invented` | названы 2025-09..12 без значений; находка `historical_gap` |
| missing ≠ 0 | `test_missing_is_not_a_zero` | подтверждённый ноль `0` и «нет данных» различаются; `None` не промоутится |
| конфликты видны и не закрываются проверкой | `ConflictTests` | расхождение вариантов книги записано открытым; проверка не закрывает, закрытие конфликта — закрывает (`resolved` в аудите); «часть больше целого» считается расхождением |
| прототипы и неоднозначный год — не факты | `test_prototype_and_ambiguous_observations_cannot_be_promoted`, `test_an_unresolved_observation_is_not_counted_anywhere`, `test_a_published_observation_can_be_promoted...` | `promote` отказывает с причинами; наблюдения без источника не входят в суммы; опубликованный целый период промоутится и это аудируется |
| ingest идемпотентен | `test_ingest_is_idempotent` | повторный импорт не удваивает наблюдения |

### E2E — сквозная цепочка по реальному HTTP

`tests/test_admin_e2e.py` поднимает два WSGI-сервера на loopback (публичный сайт
и закрытый стол) и проходит цепочку синтетическим браузером без авто-редиректов:

1. сбор (ночной цикл из офлайн-фикстур) → публичный сайт отдаёт живой выпуск;
2. без сессии стол недоступен (303 на `/admin/login`);
3. полноценный OIDC-редирект на провайдера → callback с `code`/`state` → сессия;
4. `verify` периода с CSRF-токеном со страницы; чужой `Origin` → 403;
5. исправление новости (ручная версия, `manual_state=edited`);
6. закрытие периода (v1) и корректировка (v2 с причиной);
7. публичный сайт показывает обе версии и причину, стол на публичном порту — 404;
8. экспорт Markdown без внутренних идентификаторов;
9. восстановление из консистентной копии SQLite (`Connection.backup`), а не копированием файла: версии и ручная правка на месте, публичный сайт их отдаёт;
10. выход отзывает сессию, cookie гасится (`Max-Age=0`).

Второй тест проверяет, что страница «Публикация» называет плоскость и не
показывает числа обеих плоскостей одновременно.

## Ручная проверка (вне unittest)

* CLI прогнан на staging-базе: `bootstrap` → `member add/remove` → `sessions` →
  `history` → `diagnose` → `close` → `post`; все команды завершились `0`, вывод
  JSON/Markdown без секретов.
* `python3 -m compileall -q randreport tests` — ok.
* `openssl`-кросс-проверка подписей — см. выше.

## Пробелы и ограничения (честно)

1. **Реальный Telegram OIDC не подключён.** Проверяется синтетический провайдер
   (свои ключи, свой issuer/audience, свой redirect allowlist). Нужны креды и
   согласованный список коммунаров — интеграция помечена blocked.
2. **Скриншотов стола нет.** Закрытый стол открывается только после входа через
   Telegram; визуальная приёмка (снимки, мобильные вьюпорты, клавиатура)
   вынесена в задачу независимой проверки. Публичные страницы имеют свои снимки
   в `docs/screenshots` (карточка публичного слоя).
3. **Инфографика черновика (опционально SVG/PNG)** не реализована — числа
   черновика берутся из реальных агрегатов, но картинка не строится.
4. **Правило идентичности донатеров** не восстановлено: стол даёт форму
   безопасных псевдонимов и запрет на автоматическое сопоставление людей, но
   ранжирование по людям не включается.
5. **`pyright`/`ruff` недоступны** в окружении — статический анализ ограничен
   `compileall` и диагностикой редактора.
6. **Интеграции источников** (Zenmoney/Teamup/Telegram/LLM) остаются
   синтетическими: это данные staging, а не коннекторы с реальными ключами.

## Что тесты доказывают про приватность

* В HTML/JSON/CSV стола и публичного слоя нет `tx_/acc_/cat_/occ_/pair_/sub_/cal_`
  идентификаторов, адресов электронной почты и точного времени, кроме
  документированного «03:00».
* Операции личных и карантинных счетов не появляются даже внутри стола.
* Экспорт отчёта не содержит ни транзакций, ни внутренних ключей.
* Имена администраторов не попадают в публичную выдачу (журнал стола — только
  внутренний, и он же не содержит секретов).
