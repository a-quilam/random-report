# Результаты: финансовая политика реального nightly (F08)

Карточка: «Финансовая политика реального nightly и сверка периодов», ветка
`rel/finance`, baseline `b3c25da`. Всё проверено только на синтетике: приватная
БД и приватная карта контура синтетические, источник — локальный fixture-HTTP на
`127.0.0.1`, реальные секреты и файлы не читались, внешних запросов нет.

## Что было сломано

1. **Штатный CLI строил релиз с зашитой пустой картой** (`ScopePolicy(version=
   "env-policy-unset")`): ни один счёт и ни одна категория не подтверждены, значит
   все движения публичного контура уходили в карантин, а раздел денег показывал
   **нули** вместо подтверждённых сумм. При этом веб-сборка и редакционный стол
   пользовались картой из приватной БД — два разных ответа на один вопрос.
2. **Реальная схема `/v8/diff/` читалась неверно.** Транзакция Zenmoney всегда
   называет оба счёта (`incomeAccount` и `outcomeAccount`), и у обычного расхода
   или дохода это **один и тот же** счёт. Старый нормализатор считал любую строку
   с двумя счетами переводом: каждый расход/доход/возврат превращался в
   `internal_transfer` и исчезал из сумм. Фикстуры скрывали дефект, потому что
   ставили только одну сторону.
3. **Ликвидность счёта подразумевалась.** Снимки остатков вообще не писались
   (`balance_reader=None`), а `is_liquid` по умолчанию `True`: принадлежность
   счёта контуру автоматически делала его ликвидным.

## Что сделано

| # | Изменение | Где |
|---|---|---|
| 1 | Один общий загрузчик карты контура: свежая версия из приватной БД (приоритет, только чтение — редакционные изменения не затираются), иначе приватный файл `RANREPORT_FINANCE_POLICY` / `--policy`, иначе `contour_confirmed=False` | `projection/policy.py` (новый), `ops/cli.py`, `web/build.py` |
| 2 | `contour_confirmed=False` ⇒ все денежные показатели `None`, `completeness="unknown"`, метка `contour_unconfirmed` (+`period_incomplete`); пустая карта больше не ноль и не донат | `domain/finance.py`, `projection/release.py` |
| 3 | Разбор реальной схемы по счетам: один счёт ⇒ одно движение (расход/доход/возврат покупки), разные счета ⇒ перевод с собственными суммами и валютами каждого крыла; `deletion[]` помечает удалённое; `hold` — видимо, но не в фактических суммах до расчёта источника | `connectors/zenmoney.py` |
| 4 | Снимки остатков с явной ликвидностью по типу счёта: `cash/ccard/checking/emoney` — ликвидные, `deposit`/архив/неизвестный тип — нет, `loan`/`debt` — не деньги контура; принадлежность контуру задаёт только `scope` | `connectors/zenmoney.py` (`account_balances`), `ops/run.py` (`PolicyBalanceReader`) |
| 5 | Один запуск публикует все целые периоды с данными, их годы и текущий незавершённый месяц (последним, поэтому `current` на нём); годовое покрытие — отношение годовых сумм | `ops/pipeline.py` (`build_releases`), `ops/cli.py` |

## Матрица проверок

| Требование карточки | Тест | Результат |
|---|---|---|
| Карта из БД влияет на проекцию | `test_the_confirmed_map_drives_the_published_money` | 100000 расходов, 60000 компенсации, 30000 unclassified |
| Карта из приватного файла (БД пуста) | `test_a_private_mapping_file_drives_the_projection` | `policy_version=file-policy-1`, суммы совпадают |
| Неверный путь к файлу карты | `test_a_missing_mapping_file_is_refused_without_a_contour` | exit 5, ничего не публикуется |
| Правка стола меняет проекцию, но не затирается | `test_a_desk_mapping_edit_changes_the_projection_on_the_next_run` | 100000→130000 в `operating_expense`, unclassified исчез |
| Повтор после изменения и удаления | `test_a_repeat_run_after_an_edit_and_a_delete_follows_the_source` | 100000→120000, компенсация 0, unclassified 30000 |
| Месяц и год сходятся | `test_month_and_year_agree` | июнь+июль == год по 4 полям (140000/60000) |
| Все целые периоды + текущий незавершённый | `test_the_default_run_covers_every_whole_period_and_the_current_one` | `2025-06, 2025-07, 2025, 2026, <текущий месяц>`; текущий и его год помечены `period_incomplete` |
| Ноль записей в API | `test_the_api_receives_zero_write_requests` | все запросы — POST `/v8/diff/` с телом ровно из двух меток |
| Приватные ID не выходят в public | `test_no_private_identifier_reaches_the_public_payload` (+ `find_leaks`) | 0 совпадений в 4 разделах на 2 релизах |
| Без подтверждённой карты — nullable, не нули | `test_without_a_confirmed_map_the_nightly_publishes_unknown_not_zero` | расход/компенсация `null`, `completeness=unknown`, `contour_unconfirmed` |
| Неизвестная статья не донат | `test_an_unmapped_category_never_reaches_the_donation_bucket`, `TestUnknownCategoryIsNotADonation` (3) | bucket `unclassified`, компенсация 60000 без 30000 |
| Реальная схема: money/transfer/refund | `TestZenmoneyRealSchema` (9) | 1 движение на расход/доход/возврат; перевод = 2 крыла с разными валютами; `deletion[]`; `hold` |
| Только чтение (нулевые записи) | `TestS01ReadOnlyRequest` (4, существующие) | тело — только `{currentClientTimestamp, serverTimestamp}` |
| Принадлежность ≠ ликвидность | `TestAccountLiquidity` (4) | публичный `deposit` исключён (`not_liquid_excluded`), баланс 75000 |
| Стол не перезаписывается ночью | `test_the_nightly_never_rewrites_the_desk_mapping` | 1 версия карты, 0 аннотаций |
| Балансы пишутся с явной ликвидностью | `test_balances_are_captured_with_an_explicit_liquidity` | `acc_card` 500000, `is_liquid=1` |
| `--dry-run` читает, но не публикует | `test_a_dry_run_reads_the_sources_but_publishes_nothing` | 0 релизов, 3 факта в приватной БД |

## Полный прогон

```
python3     -W error::ResourceWarning -m unittest discover -s tests -t .
python3.14  -W error::ResourceWarning -m unittest discover -s tests -t .
```

* Python 3.13.5: `Ran 407 tests ... OK`, ресурс-гвард чист;
* Python 3.14.9: `Ran 407 tests ... OK`, ресурс-гвард чист;
* `python3 -m compileall -q randreport tests` — без ошибок;
* новые тесты: `tests/test_f08_nightly_policy.py` (42 теста).

Дополнительно на синтетической БД прогнана веб-сборка:

```
python3 -m randreport.web build --db ... --release-dir ... --periods 2025-07,2025
python3 -m randreport.web check --release-dir ... --db ...
=> {"pages_checked": 120, "problems": [], "problem_count": 0}
```

## Границы

* Реальный Zenmoney/Teamup/Telegram не опрашивались: интеграции остаются
  `blocked` до выдачи доступов (`docs/INGEST.md` §8). Схема сверена с
  документацией ZenPlugins в момент реализации, но живой payload не проверялся.
* Валюта счёта определяется по массиву `instrument` payload'а и/или явной карте
  инструментов; неизвестная валюта даёт состояние неполноты, а не догадку.
* Обязательства (`loan`/`debt`) не попадают в ликвидный баланс, но отдельная
  публичная строка обязательств по-прежнему заполняется только внешним вводом
  (`ReleaseBuilder(liabilities=...)`), как и раньше.
* Единственная минорная единица — та, что принята проектом (`docs/INGEST.md`
  §2.1: для рубля это копейки); масштаб суммы из API здесь не пересматривался.
