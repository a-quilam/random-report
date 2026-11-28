# Результаты тестов: непрозрачная ревизия Teamup и идентичность календаря

Задача `t_c2ea6720`, ветка `rel/calendar-adapter`, базовый коммит
`b3c25da`. Дефект воспроизведён в реальном read-only staging: `TeamupClient.
_normalize_event` делал `int(raw["version"])`, а API возвращает непрозрачную
строку (hex), из-за чего импорт календаря останавливался. Здесь же закрыты
следствия: семантика правок, переносов, tombstone/retention, повторных доставок
и инвалидации отметок для неупорядоченных ревизий, и разделение секретной ссылки
календаря и устойчивой внутренней идентичности при ротации ключа.

Только синтетика: выдуманные id, выдуманные calendar key, синтетические
полезные нагрузки. Реальных креденшелов, id и данных нет; сеть в тестах не
используется.

## 1. Воспроизведение дефекта на исходном коммите (до кода)

Новый набор `tests/test_e02_opaque_revision.py` прогнан на `b3c25da` в отдельном
worktree с тем же набором (импорты констант заменены на безопасные заглушки,
чтобы измерялось поведение, а не `ImportError`):

```
FAILED (failures=5, errors=18)      # 23 из 24 тестов на исходном коде
```

Прямое воспроизведение остановки импорта (`version = "6a1f2c9d4b"`):

```
FAILED: ValueError invalid literal for int() with base 10: '6a1f2c9d4b'
```

Секретная ссылка вместо внутренней идентичности на том же коммите:

```
client.calendar_id == SECRET-CALENDAR-KEY-77
stored calendar_id: SECRET-CALENDAR-KEY-77
desk row key: SECRET-CALENDAR-KEY-77|occ_1
```

Числовое сравнение ревизий в домене на непрозрачных токенах (`ffffffff`
доставлена первой, `00000001` — последней, запись перенесена с 3 на 4 июля):

```
kept start: 2025-07-03 rev: ffffffff
superseded: ['00000001']
```

То есть в оперативную статистику попадала *устаревшая* доставка только потому,
что её токен лексикографически/численно больше.

## 2. Прогон после исправления

```
cd /home/agent/projects/random-report-calendar-adapter
python3    -W error::ResourceWarning -m unittest discover -s tests -t .   # 389 OK
python3.14 -W error::ResourceWarning -m unittest discover -s tests -t .   # 389 OK
python3 -m compileall -q randreport tests                                 # ok
```

`389 = 365 прежних + 24 новых` в `tests/test_e02_opaque_revision.py`. Ни одного
`ResourceWarning`, страж ресурсов (`randreport/leakguard.py`) не сработал.

Синтетический сквозной прогон публичного слоя (та же схема, что и в приёмке
`§§6–10`; приватная БД и release-каталог — во временном каталоге вне репозитория):

```
python3 -m randreport.web build --periods 2025-07,2025 --release-dir <tmp>
  -> {"published": [["2025-07", "rel_d462a596c87bb574"], ["2025", "rel_a6785a2a3101843c"]]}
python3 -m randreport.web check --release-dir <tmp>
  -> {"pages_checked": 120, "problems": [], "problem_count": 0}
```

Проверка утечек на этом же прогоне (событие доставлено с непрозрачной ревизией
`6a1f2c9d4b`, calendar key — `synthetic-secret-key`):

| Что ищем | Приватная БД | Опубликованные файлы |
|---|---|---|
| секретный calendar key | отсутствует | отсутствует |
| внутренний `calendar_id` | присутствует (ожидаемо) | отсутствует |
| непрозрачная ревизия | присутствует (ожидаемо) | отсутствует |

## 3. Клаузы карточки → тесты

| Клауза карточки | Где проверяется |
|---|---|
| hex-ревизия не останавливает импорт, значение сохраняется | `OpaqueRevisionTokenTests.test_a_hex_revision_is_accepted_and_kept_verbatim` |
| «не сводить значения к 1» | `test_a_missing_revision_gets_the_documented_token_and_not_one`, `test_a_token_with_leading_zeros_is_not_coerced_into_a_number` |
| «не считать hash временем» | `test_a_token_that_looks_like_an_epoch_is_not_read_as_time`, `UnorderedRevisionTests.test_the_last_delivery_wins_even_when_its_token_sorts_lower` |
| правки и переносы | `OpaqueRevisionEndToEndTests.test_two_opaque_revisions_and_a_repeat_travel_the_whole_path` |
| семантика для неупорядоченных ревизий | `UnorderedRevisionTests` (4 теста) |
| tombstone | `...test_a_tombstone_with_an_opaque_revision_keeps_the_row_and_is_idempotent`, `test_a_tombstone_needs_no_revision_token`, `test_a_tombstone_without_an_id_is_a_protocol_error` |
| retention | `...test_retention_keeps_the_opaque_token_and_does_not_reset_it`, `test_a_record_the_source_returns_again_leaves_the_archive` |
| повторные доставки | `test_two_opaque_revisions_and_a_repeat_travel_the_whole_path` (шаги «повтор»), `UnorderedRevisionTests.test_a_repeated_delivery_of_the_same_token_is_collapsed` |
| инвалидация отметок | тот же сквозной тест: `checks.VERIFIED` → новая ревизия → `checks.STALE` → повтор ревизии не сбрасывает свежую отметку |
| raw → SQLite → sync → public | `stored()` (тип `text`, токен дословно) + `events.json` релиза в том же тесте |
| отделение секретной ссылки от идентичности | `CalendarIdentityTests` (5 тестов) |
| форма протокола | `test_a_non_scalar_revision_is_a_protocol_error` |
| миграция на существующей БД | `RevisionColumnMigrationTests` (2 теста) |

## 4. Дефекты, найденные и исправленные

1. **Остановка импорта на непрозрачной ревизии.** `int(raw["version"])` заменён
   на непрозрачный токен (`parse_revision`): строка сохраняется как есть, число
   из JSON — как его текстовая форма, отсутствие поля — документированный
   `unversioned`, не скаляр — отказ протокола.
2. **Ревизия не входила в `content_hash`.** Правка на источнике с теми же
   нормализованными полями не переписывала строку, поэтому отметка проверки
   никогда не становилась `stale` — инвалидация отметок не работала. Теперь
   токен входит в хэш: смена ревизии переписывает строку, повтор той же ревизии
   идемпотентен.
3. **Числовое сравнение ревизий в домене.** `select_completed` выбирал «старшую»
   ревизию; для непрозрачных токенов это давало устаревшую запись в статистике
   (показано в §1). Разрешение переведено на порядок доставки: выигрывает
   последняя доставка ключа, прежние остаются в `revision_log` и
   `duplicates_collapsed`.
4. **Колонка ревизии имела числовой тип.** `INTEGER`-колонка (NUMERIC affinity)
   молча переписывала числовые токены: `0007` → `7`, 20-значный токен →
   float. Добавлена миграция 4: колонка перестроена в `TEXT`, существующие
   значения перенесены как текст (проверено на схеме 3 с данными).
5. **Секретный ключ был внутренней идентичностью.** `calendar_id` возвращал
   секретный calendar key, поэтому секрет попадал в приватную БД, в ключи
   отметок проверки и в админский стол (`desk row key`), а ротация ключа
   распадалась на два календаря с удвоением статистики. Введён отдельный
   устойчивый `calendar_id` (`TEAMUP_CALENDAR_ID`, по умолчанию `cal_teamup`),
   секрет в этой роли отвергается; ротация ключа сохраняет одну запись, историю
   и отметку проверки.
6. **Tombstone без `id`** давал строку с идентификатором `"None"` вместо отказа
   протокола.
7. **`local_archive` не сбрасывался**, если источник снова вернул запись: строка
   одновременно считалась «присутствует» и «в архиве», и агрегат
   `local_archive_events` завышался. Теперь архив означает текущее исчезновение.

## 5. Границы проверки

* Живой Teamup не читался: реальный `calendar key` и боевые id в задаче
  отсутствуют, доступ остаётся `blocked` (`docs/INGEST.md` §8). Форма ответа
  смоделирована по документированной схеме (`version` — строка).
* Ротация ключа проверена на двух выдуманных секретах с одним внутренним id;
  ротация на реальном календаре не выполнялась и не требуется.
* Миграция проверена на копии схемы 3 с одной строкой данных; боевая БД не
  трогалась.
* Найдено вне рамок задачи (не исправлялось здесь): `python -m randreport.web
  build` без `--db`/`RANREPORT_DB` создаёт в текущем каталоге файл с именем
  `None` вместо отказа — публичный слой, отдельная карточка. Исправлено в
  карточке `t_3eeb34dc` (отчёт — `docs/TEST_RESULTS_WEB_BUILD.md`).
