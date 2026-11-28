# Домен, контракт сборщика и SSR

Документ фиксирует контракт между доменным слоем (`randreport/domain`), слоем
проекции (`randreport/projection`), ночным сборщиком и публичным SSR.
Источник требований — `docs/REQUIREMENTS.md` §§6–7, 10, 13. Ничего, кроме
перечисленного ниже, публичный слой отдавать не имеет права.

## 1. Карта модулей и импортов

```
randreport/domain/money.py        копейки, Decimal, запрет float, «неизвестно ≠ 0»
randreport/domain/timeframes.py   Period, Europe/Moscow, границы целых месяцев/лет
randreport/domain/flows.py        EconomicClass, AccountScope, направления
randreport/domain/finance.py      Movement/ScopePolicy -> ClassifiedLedger -> FinanceTotals, баланс, сверка
randreport/domain/events.py       Occurrence/EventTypeMapping -> EventSelection, EventStats
randreport/domain/dto.py          StrictDTO и allowlist-контракт публичных полей
randreport/projection/query.py    allowlist параметров, resolve_period, granularity
randreport/projection/privacy.py  find_leaks/assert_no_leaks/public_json
randreport/projection/release.py  ReleaseContext, build_money_payload, build_events_payload, release_id_for
randreport/projection/csv_export.py export_categories_csv, CSV_HEADER
```

Импорт — только через пакет `randreport` (репозиторий запускается из корня).
Внутренние идентификаторы (`Movement.id`, `category_id`, `account_id`,
`Occurrence.occurrence_id`, `calendar_id`, `subcalendar_ids`) не покидают
`randreport.domain`: у публичных DTO нет полей, куда их можно положить.

## 2. Деньги

* Все суммы — целые копейки (`int`). `parse_rub` принимает `int` (рубли),
  `str`/`Decimal` (рубли с копейками), округление `ROUND_HALF_EVEN` до копейки.
  `float`/`bool` → `MoneyTypeError`. Рубли только на отображение:
  `format_kopecks`, `CategoryDTO.amount_rub`.
* «Неизвестно» — это `None`, а не `0`. Подтверждённый ноль — это `0`.
  Неизвестная доля, отсутствующая субсидия, отсутствие снимка баланса, валюта
  без курса — всё это `None` плюс явная метка.
* `share_percent(part, whole)` → `None` при `whole == 0` («не применимо»),
  округление `ROUND_HALF_UP` до 1 знака. Годовое покрытие — отношение годовых
  сумм (`weighted_annual_coverage`), не среднее месячных процентов.
* Целое не рассчитывается из float ни на одном шаге.

## 3. Периоды и параметры запроса

`Period` = `month:YYYY-MM`, `year:YYYY`, `range:YYYY-MM..YYYY-MM` (только целые
месяцы) или `all_time`. `parse_period` не принимает день/произвольные даты
(`UnsafePeriodError`). Дата учёта — фактическая дата операции в Europe/Moscow
(`msk_day`), не дата редактирования.

Публичный allowlist параметров (`projection/query.py`):
`period` (обязателен; `YYYY-MM` | `YYYY` | `all`), `granularity`
(`month` | `year`), `unit` (`RUB_kopecks`). Любой другой параметр, день,
диапазон внутри месяца, несовпадающий `granularity` или `unit` → `UnsafeQueryError`
до чтения данных. CSV — только целые месяц или год и только период релиза.

## 4. Публичный контур и классы потоков

`ScopePolicy` (версионируемая): `public_accounts`, `private_accounts`,
`quarantined_accounts`, `category_classes`, `category_labels`,
`quarantined_categories`, `commission_categories`, `subsidy_category_ids`,
`count_visibility_threshold_kopecks`, `sensitive_categories`.

* Счёт неизвестной/смешанной принадлежности → карантин, суммы не публикуются
  (метка `quarantine_present`, `completeness=partial`).
* Счёт вне контура (личное/жилое) → исключён полностью (`excluded_ids`).
* Новая категория → класс `unclassified`, отдельная строка
  `bucket="unclassified"`, метка `unclassified_present`; в `E`/`C`/`S`
  автоматически **не** попадает (решение зафиксировано ниже, §9). Неизвестная
  статья никогда не считается донатом (`external_compensation`) и не
  приписывается к субсидии.
* `contour_confirmed=False` — контур публичных счетов не подтверждён вовсе (нет
  записанной версии карты и нет приватного файла). Тогда ни одно из чисел не
  публикуется: все показатели `None`, `completeness="unknown"`, метка
  `contour_unconfirmed` (+ `period_incomplete` для незавершённого периода).
  Карта грузится одним общим загрузчиком (`projection/policy.py`) для ночного CLI,
  веб-сборки и редакционного стола; приоритет — записанная столом версия
  (загрузка только читает), затем приватный файл `RANREPORT_FINANCE_POLICY`.
* `EconomicClass`: `external_compensation`, `commune_subsidy`,
  `operating_expense`, `expense_refund`, `compensation_refund`, `financing_in`,
  `financing_out`, `internal_transfer`, `balance_adjustment`, `unclassified`.
* Два «крыла» одного перевода (`pair_kind="transfer_leg"`, общий `pair_id`)
  сворачиваются в одну запись `internal_transfer` (`duplicated_legs`,
  `internal_transfer_pairs`); `cross_currency`/`pair_amount_mismatch` — метки,
  не догадки. Комиссия — отдельная операция-расход.
* Прямая оплата коммунаром + возмещение (`reimbursement_of`, `pair_kind="reimbursement"`)
  → одна расходная операция и один вклад (`commune_subsidy`), без удвоения.
* Плановые (`is_planned`) и удалённые (`deleted`) движения в суммы не входят;
  их id остаются в `planned_ids`/`deleted_ids`.

## 5. Формулы и статусы

`E = gross_expense − expense_refunds`, `C = external_compensation −
compensation_refunds`, `S = фактический вклад (nullable)`,
`D = E − C` (signed), `coverage = 100·C/E` при `E > 0`, иначе `None`,
`shortfall = max(D,0)`, `excess = max(−D,0)`,
`result_before = C − E`, `result_after = C + S − E` (nullable без `S`).

`FinanceTotals.completeness` ∈ `complete | partial | unknown`.
`labels` (машиночитаемые, стабильные):
`no_data`, `subsidy_unknown`, `coverage_not_applicable`,
`expense_refunds_exceed_expenses`, `negative_net_compensation`,
`quarantine_present`, `unclassified_present`, `orphan_transfer_leg_present`,
`cross_currency_transfer_present`, `pair_amount_mismatch_present`,
`financing_present`, `period_incomplete`.
`source_data_present=False` → все показатели `None` + `no_data`
(источник не читался — это не ноль). Полностью прочитанный пустой источник →
подтверждённые нули, `completeness=complete`, без `no_data`.
`contour_confirmed=False` → все показатели `None` + `contour_unconfirmed`
(карта публичного контура отсутствует: неизвестность контура не сумма, и тем
более не ноль). Оба случая несут `coverage_is_final=false`.

## 6. Баланс (`F07`)

`consolidated_balance(balances, as_of=…, rates=…, liabilities=…, scope_policy=…)`:

* только подтверждённые, ликвидные, публичные снимки; снимок нужной даты, иначе
  `None` + `unknown_historical_balance` (текущий баланс в историю не подставляется);
* ликвидность счёта задаётся самим счётом, а не его принадлежностью контуру:
  счёт, подтверждённый как публичный, но неликвидный (вклад, закрытый счёт,
  неизвестный тип) исключается с меткой `not_liquid_excluded`, а счёт типа
  `loan`/`debt` не прибавляется к деньгам контура вовсе;
* `RUB` суммируется, иная валюта — только при явном курсе (`converted`);
  валюта без курса → `balance_incomplete` и `None` (не частичная сумма);
* долги/неликвид не прибавляются: `obligations_kopecks` отдельно;
* дефицит (`D`) никогда не является балансом; подтверждённый нулевой баланс — `0`.

## 7. События (`E01–E03`)

* Состоявшееся: `end <= cutoff` при наличии действующей записи (включая
  локально заархивированную после retention; явное удаление исключается из
  оперативной статистики, ревизия сохраняется).
* Ключ учёта — `(calendar_id, occurrence_id)`, повторяющиеся события считаются
  по экземплярам, запись в нескольких subcalendar — один раз, перенос — новая
  ревизия без дубля. `calendar_id` — устойчивая внутренняя идентичность
  календаря, не секретный ключ (ротация ключа не создаёт второй календарь). Тип и
  приватность — только по устойчивым subcalendar ID
  (`EventTypeMapping`), конфликт → приоритет приватности; «отмена» в названии не
  меняет тип, а попадает в `admin_flags`.
* Ревизия — непрозрачный токен источника: она не сравнивается и не упорядочивает
  ничего. Если один ключ доставлен несколько раз (перекрывающиеся окна, повтор),
  считается последняя доставка; прежние остаются в `revision_log` и в
  `duplicates_collapsed`. Новая ревизия меняет строку и `revision_hash` отметки
  проверки, поэтому старая отметка становится `stale`, а повторная доставка той же
  ревизии идемпотентна.
* Тип: `open`, `open_by_registration`, `closed`, `internal_meeting`,
  `preparation`, `unknown`, плюс производные категории закрытого контура
  `meeting` и `closed_rent`. Неизвестный тип попадает в общий счёт и «тип не
  указан», авто-открытым не считается.
* Закрытый контур настраивается устойчивыми ID (`EventTypeMapping`):
  `meeting_subcalendars` (разрешённый контур собраний), `rent_subcalendars`
  (закрытые аренды), `excluded_subcalendars` (исключено полностью). Точное
  название «Собрания» в разрешённом контуре → `meeting`; остальные закрытые —
  `closed_rent` по маппингу либо `closed`. Исключённый подкалендарь не даёт ни
  списков, ни итогов (бакет `EventSelection.excluded`, только внутренний).
  Конфликт/неизвестность — консервативно (точное совпадение, приоритет приватности,
  пересечение контура собраний и аренды → агрегат). Подробности — docs/CALENDAR.md.
* В публичный список (`public_events`) и в дневную сетку (`calendar_days`) идут
  только `open` и `open_by_registration`. Закрытые/внутренние/подготовка/unknown и
  `closed_rent` — только агрегаты (`event_type_aggregates`), без названий, дат, id,
  длительностей и точек сетки. Собрания (`meeting`) по решению владельца публикуются
  как `public_meetings`/`meeting_days`: только московская дата начала и постоянная
  подпись «Собрание» (`PublicMeetingDTO`), без названия/времени/id/участников.
  Весь раздел относится к периоду релиза: состоявшаяся запись входит в период своего
  **московского дня начала** (границы целого месяца/года), а не в «всё время» —
  иначе месячный и годовой релизы показывали бы одинаковые числа. `all_time`
  оставляет всё.
  Дневная сетка отдаётся только для одного месяца
  (`calendar_granularity="day"`), для года/диапазона/всего — `"month"` и пустые
  `calendar_days`/`meeting_days`. Форма регистрации прошедшего события не
  показывается (`registration_open=false`).

## 8. Публичный allowlist DTO (`domain/dto.py`)

`StrictDTO`: неизвестные поля → `UnknownFieldError`, `float` → `InvalidFieldError`,
иначе `TypeError`-совместимый `DTOError`. `to_public_dict()`/`from_public_dict()`
дают полный round-trip; SSR обязан сериализовать только эти объекты
(`projection.privacy.public_json`).

* `PeriodDTO`: `requested`, `covered_start`, `covered_end`, `complete`
* `CategoryDTO`: `key`, `label`, `bucket`, `direction`, `amount_kopecks`,
  `amount_rub`, `share_percent`, `operations_count`
  (`operations_count=None`, если сумма ниже порога или категория чувствительная)
* `MoneyDTO`: `period`, `as_of`, `release_id`, `freshness`, `verification`,
  `completeness`, `source_labels`, `unit`, `metrics`, `categories`
* `PublicEventDTO`: `on`, `type`, `title`, `description`, `announce_url`,
  `registration_open`
* `PublicMeetingDTO`: `on`, `label` — собрание: только московская дата начала и
  постоянная подпись «Собрание»; других полей у объекта нет
* `EventTypeAggregateDTO`: `type`, `count`, `public`
* `EventsDTO`: `period`, `as_of`, `release_id`, `freshness`, `verification`,
  `completeness`, `source_labels`, `public_events`, `meetings`, `event_type_aggregates`,
  `event_type_details`, `calendar_days`, `meeting_days`, `calendar_granularity`, `metrics`
* `ChannelPointDTO`: `on`, `subscribers`, `source`; `CommunityDTO`:
  `period`, `as_of`, `release_id`, `freshness`, `verification`, `completeness`,
  `source_labels`, `channel_points`, `metrics`
* `NewsItemDTO`: `id`, `title`, `summary`, `category`, `published_on`,
  `source_urls`, `machine_processed`, `verified`; `ChronicleDTO` аналогично
  `CommunityDTO` с `news_items`
* `metrics` (`MoneyDTO`): `gross_expense_kopecks`, `expense_refund_kopecks`,
  `expense_kopecks`, `external_compensation_kopecks`,
  `compensation_refund_kopecks`, `compensation_kopecks`, `subsidy_kopecks`,
  `uncovered_kopecks`, `coverage_percent` (строка или `null`),
  `coverage_is_final`, `shortfall_kopecks`, `excess_kopecks`,
  `result_before_subsidy_kopecks`, `result_after_subsidy_kopecks`,
  `unclassified_in_kopecks`, `unclassified_out_kopecks`,
  `financing_in_kopecks`, `financing_out_kopecks`,
  `balance_adjustment_kopecks`, `internal_transfer_kopecks`,
  `category_sum_kopecks`, `status_labels`, `contour_confirmed`, а при известном
  балансе `balance_kopecks`, `balance_completeness`, `obligations_kopecks`,
  `balance_status_labels`
* `metrics` (`EventsDTO`): `total_events`, `public_events`, `closed_aggregate`,
  `unknown_type_events`, `local_archive_events`, `public_share_percent`
* `key` категории — необратимый публичный идентификатор
  (`category_public_key`), исходный `category_id` не публикуется.

## 9. Контракт сборщика и SSR

Сборщик (ночной цикл 03:00 Europe/Moscow) на каждый источник вызывает только
доменные функции, затем публикует один релиз:

```python
ledger = build_ledger(movements, policy, period, source_data_present=…)
totals = compute_totals(ledger, as_of=cutoff_date)      # subsidy=<SubsidySignal> при внешнем вводе
checks = reconcile_totals(ledger, totals) + reconcile_year_vs_months(months, year)
balance = consolidated_balance(balances, as_of=…, rates=…, liabilities=…)
ctx = ReleaseContext(release_id=…, as_of=cutoff, period=period,
                     freshness=…, verification=…, completeness=…, source_labels=…)
money = build_money_payload(ctx, totals, balance=balance)     # MoneyDTO
events = build_events_payload(ctx, selection)                  # EventsDTO
text = public_json(money)                                      # проверяет утечки
csv_text = export_categories_csv(money, params={"period": period.label})
```

* `release_id` — содержимо-адресный (`release_id_for(payload)` = `rel_<sha256[:16]>`);
  публикация атомарна, устаревший релиз не перезаписывается, при частичном сбое
  раздела отдаётся последний валидный релиз с меткой `freshness`.
* SSR читает только построенную проекцию (SQLite/JSON-снапшот релиза) и
  рендерит только DTO из §8. Публичные страницы не ходят в источники и не
  считают финансы.
* API отдаёт `GET /api/public/{money,events,community,chronicle,overview}` с
  параметрами из §3; `GET /api/public/export/categories.csv` — CSV из §10.
  HTML, JSON, CSV и скачиваемые отчёты проходят один и тот же
  `assert_no_leaks`; ошибки наружу не содержат внутренних идентификаторов.
* `checks` из сверки — не «ошибки сайта», а публикуемые статусы: расхождение
  год/месяцы, конфликт с исторической публикацией (`compare_historical`,
  `code="historical_conflict"`), появление новых категорий. Историческая ошибка
  фиксируется, исходная публикация не переписывается, фиктивные операции не
  создаются.

## 10. CSV (R02)

`CSV_HEADER = period,category_key,category_label,bucket,direction,amount_kopecks,
amount_rub,share_percent,unit,as_of,release_id,completeness`.

Только категории разрешённых целых периодов. Нет `transaction_id`,
`account_id`, контрагентов, дневных сумм, описаний и данных календаря.
Нестандартные параметры отклоняются (`UnsafeQueryError`), столбцы не
расширяются, содержимое проверяется `assert_no_leaks` перед отдачей.

## 11. Зафиксированные решения (отступления от буквального чтения требований)

1. **`unclassified`/карантин не входят в `E`/`C`/`S`.** Новая или неясная
   категория публикуется отдельной строкой и снижает `completeness` до `partial`
   (`coverage_is_final=false`), но не приписывается автоматически ни к расходу,
   ни к компенсации, ни к субсидии.
2. **Внутренние переводы не входят в `category_sum_kopecks`.** Они не меняют
   деньги контура; видны отдельно (`internal_transfer_kopecks`, `pair_id`-списки).
3. **Субсидия известна, если в политике заданы категории вклада.** Иначе
   `S=None` + `subsidy_unknown` (неизвестность не ноль).
4. **Одна валюта без курса обнуляет консолидированный баланс** (не частичная
   сумма) + `balance_incomplete`.
5. **Публичный список событий = только `open` и `open_by_registration`.**
   Подготовка и unknown идут в агрегаты, но не в список и не в дневную сетку.
6. **Число операций скрывается** для сумм ниже порога и для чувствительных
   категорий.
7. **Минимальная публичная гранулярность периода — один календарный месяц**;
   дневная сетка календаря — только внутри одного месяца.
8. **Собрания — единственное исключение из «закрытое не попадает в сетку».** По
   решению владельца запись разрешённого закрытого контура, название которой
   удовлетворяет настроенному правилу собрания, становится типом `meeting` и
   публикуется как московская дата начала плюс постоянная подпись «Собрание»;
   исходное название заменяется константой, остальные закрытые (включая закрытые
   аренды) остаются только агрегатами. Правило конфигурируется устойчивыми ID;
   режим сопоставления названия — явный (`exact` по умолчанию, `contains` —
   записанный владельцем вариант, см. решение 14 ниже), не подстрока по
   умолчанию и не цвет. См. docs/CALENDAR.md.
9. **Непрозрачная ревизия разрешается по порядку доставки.** Числовое сравнение
   `version` (в том числе base16 или epoch) отброшено: API не документирует
   порядок токена. Выигрывает последняя доставка ключа — тот же порядок, в
   котором строки ложатся в БД (одна строка на ключ).
10. **Отметка проверки привязана к токену ревизии, а не к «номеру».** Поэтому
   правка источника инвалидирует отметку даже при совпадающих нормализованных
   полях; повторная доставка той же ревизии отметку не сбрасывает.
11. **Ночной цикл публикует несколько периодов за один запуск.** Без `--period`
   это все целые месяцы, по которым есть факты, соответствующие годы и текущий
   незавершённый месяц (последним, поэтому `current` указывает на него). Годовое
   покрытие — отношение годовых сумм (`weighted_annual_coverage`), не среднее
   месячных процентов. `--period` принимает список `YYYY-MM,YYYY`.
12. **Карта контура не хардкодится.** Ночной CLI строит `ScopePolicy` тем же
   загрузчиком, что и веб-сборка/стол (`projection/policy.py`). Пока карты нет,
   `contour_confirmed=False` и все денежные показатели `None`; жёстко
   зашитого «пустого» политики (`env-policy-unset`) больше нет.
13. **Карта календаря имеет документированный приоритет и применяется циклом.**
   `projection.policy.event_mapping_for`: записанная столом версия
   (`event_type_mapping`) → приватный файл (`RANREPORT_EVENT_MAPPING` / `--events`)
   → переменные `TEAMUP_*`. До этого ночной цикл читал только БД, поэтому
   документированная поверхность `TEAMUP_*` и приватный файл молча игнорировались —
   приватные правила календаря нельзя было применить ни в staging, ни в бою без
   записи в БД. Загрузка только читает: версия стола не перезаписывается.
14. **Совпадение названия собрания — явный режим, умолчание осторожное.**
   `meeting_match`: `exact` (умолчание, совпадение целиком) или `contains`
   (записанный владельцем в `закрытый файл правил владельца` вариант `source_title_contains_word`
   при `case_sensitive: false`; сравнение **целым словом**, без учёта регистра).
   Неизвестное значение режима — отказ: опечатка в конфигурации не имеет права
   расширять публикацию. Публикация собрания не меняется: московская дата начала
   плюс константа, без исходного названия.
15. **Изменения Teamup читаются отдельным вызовом.** `modifiedSince` нельзя
   комбинировать с диапазоном дат (API отвечает 400), а в режиме изменений API
   возвращает мастер повторяющейся серии вместо экземпляров. Поэтому окно по
   датам читается целиком (экземпляры), а changelog — только для явных удалений и
   только внутри документированных 30 дней; более старый курсор обслуживается
   окном и локальным архивом. Иначе повторный ночной запуск (второй и далее)
   падал бы с HTTP 400 на каждом цикле.
16. **Раздел событий относится к периоду релиза.** Состоявшаяся запись входит в
   период своего московского дня начала (целые месяц/год; `all_time` — всё).
   Без этого все релизы несли одни и те же числа «за всё время»: закрытая
   staging показала 38 релизов разных периодов с одинаковыми агрегатами
   календаря и одинаковым списком собраний, что противоречит требованию
   фильтрации по месяцу/году (§3.3).
17. **Правила распределения подвала — версии карты, а не правки на месте.**
   Владельческие override-правила (`allocation` внутри payload карты контура)
   применяются к *классифицированному реестру* до расчёта итогов
   (`domain/allocation.apply_allocation`), поэтому агрегаты, строки категорий,
   CSV и HTML выводятся из одного реестра и разойтись не могут. Доля общих
   расходов применяется к **месячной** сумме целых копеек с явным округлением
   (`half_up` по умолчанию), поэтому год — сумма месяцев, а не доля года;
   согласованная аренда (90000 ₽/мес) **заменяет** банковский платёж и публикуется
   рядом с ним (двойного учёта нет), не экстраполируется на месяц без реальной
   аренды и не переписывает месяц раньше `from_month`; корректировки обоих знаков
   учитываются раздельно, статья без подтверждённого класса остаётся
   `unclassified` без выдуманного класса; надкатегория отдаёт только часть, не
   покрытую детьми. Исключение отдельной операции — отдельная **обратимая**
   запись стола (`finance_report_exclusions`, миграция 7) с CSRF и аудитом;
   первоисточник не меняется, операцию с неподтверждённой принадлежностью
   исключить нельзя. Правка политики не переписывает остатки и не переписывает
   неизменяемые версии отчётов. Контракт — docs/ALLOCATION.md.
