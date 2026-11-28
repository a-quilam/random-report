# Результаты тестов: отказ вместо выдуманного пути в веб-сборке

Карточка `t_3eeb34dc`, репозиторий `/home/agent/projects/random-report`, ветка
`main`, базовый коммит `b880f9a` (после интегратора `t_60dd112b`). Дефект был
найден вне рамок `t_c2ea6720` при синтетическом smoke публичного слоя: сборка
без пути к приватной БД превращала `None` в имя файла.

Только синтетика: временные каталоги, пустая мигрированная БД, выдуманные
данные. Реальных креденшелов, id и данных нет; сеть не использовалась.

## 1. Воспроизведение дефекта (до кода)

`s0` — коммит из отчёта (`0f4856b`), `s1` — слитый `main` (`b880f9a`, то есть
состояние до этой правки). Команда запускается в **пустом** каталоге вне
репозитория, `PYTHONPATH` указывает на соответствующий checkout:

```
python3 -m randreport.web build --periods 2025-07,2025      # без --db и RANREPORT_DB
```

`s0` — файл `None` и падение на публикации:

```
TypeError: argument should be a str or an os.PathLike object where
  __fspath__ returns a str, not 'NoneType'      (ops/publish.py:34, Path(root))
exit=1
-rw-r--r-- 1 agent agent 344064 Nov 22 16:23 None   # SQLite, создан connect(None)
```

`s1` — падение уже отрезано (`build_releases` пропускает публикацию при
`release_dir is None`), но дефект остался и стал **тише**: сборка «успешна», хотя
не опубликовано ничего, а БД по-прежнему с именем-строкой:

```
{"published": [["2025-07", "rel_e2693c81f43be396"], ["2025", "rel_2beee9da8aade8e7"]]}
exit=0
-rw-r--r-- 1 agent agent 376832 Nov 22 16:23 None
```

Новый набор `tests/test_web_build_inputs.py` на `s1` (до правки кода):

```
FAILED (failures=6, errors=3)          # 11 тестов
resource leak: 1 handle(s) are still open before exit   # соединение к файлу None
```

Проверялись все клаузулы карточки сразу: отказ по отсутствующей БД, отказ по
отсутствующему каталогу релиза, отсутствие посторонних файлов, отказ по
неизвестному периоду и отказы в самой библиотеке (`connect(None)`,
`publish_release(None, …)`).

## 2. Что исправлено

| Файл | Изменение |
| --- | --- |
| `randreport/web/__main__.py` | `cmd_build` разрешает оба пути («аргумент, затем окружение») и отказывает кодом 2 одной строкой в stderr (`a private store path is required (--db or RANREPORT_DB)` / `a release directory is required (--release-dir or RANREPORT_RELEASE_DIR)`) **до** открытия БД; неподдерживаемый `--periods` тоже отказывает (`unsupported period: …`, как в ночном CLI), а не падает трассировкой |
| `randreport/store/db.py` | `connect` принимает только настоящий путь (`os.fspath`); `None`, числа и прочие объекты → `ValueError` вместо `str(None)` |
| `randreport/ops/publish.py` | общий `_release_root` в `publish_release`/`latest_published_release`/`previous_published_release`/`rollback_release`: отсутствующий корень → `PublishError`, а не `TypeError` из `Path(root)` |

Корень дефекта — `str(path)` в `connect`: любое значение становилось именем
файла. Поэтому правка сделана в двух слоях (CLI и библиотека), а не только в
командной строке: тот же вызов из любого другого места теперь отказывает, а не
пишет файл.

## 3. Прогон после исправления

```
python3    -W error::ResourceWarning -m unittest discover -s tests -t .   # 599 OK
python3.14 -W error::ResourceWarning -m unittest discover -s tests -t .   # 599 OK
python3 -m compileall -q randreport tests build_archive.py history_archive.py   # ok
python3 -W error::ResourceWarning -m unittest tests.test_web_build_inputs       # 11 OK
```

`599 = 588 прежних + 11 новых` в `tests/test_web_build_inputs.py` (на `b880f9a`
тот же discovery давал 588 OK). Ни одного `ResourceWarning`, страж ресурсов не
сработал на обоих интерпретаторах.

## 4. Поведение команды (синтетика, каталог вне репозитория)

```
# 1) без --db и без --release-dir
$ python3 -m randreport.web build --periods 2025-07,2025
a private store path is required (--db or RANREPORT_DB)
exit=2
files in cwd: []                       # ни файла None, ни каталога релиза

# 2) --db есть, каталога релиза нет
$ python3 -m randreport.web build --db <tmp>/nodb.sqlite --periods 2025-07
a release directory is required (--release-dir or RANREPORT_RELEASE_DIR)
exit=2
store releases: 0                      # приватный стор не открывался и не менялся
files in cwd: [empty nodb.sqlite]      # новых файлов нет

# 4) неизвестный период
$ python3 -m randreport.web build --db <tmp>/badperiod.sqlite --release-dir <tmp>/never --periods nonsense
unsupported period: unsupported public period 'nonsense'
exit=2                                 # стор побайтно не изменился, каталог <tmp>/never не создан

# 5) документированный путь (аргументы) по-прежнему работает
$ python3 -m randreport.web build --db <stage>/randreport.sqlite --release-dir <stage>/releases --periods 2025-07,2025
{"published": [["2025-07", "rel_e196fe426050518d"], ["2025", "rel_a0110dd223c16c88"]]}
exit=0

# 6) то же только из окружения
$ RANREPORT_DB=<stage>/randreport.sqlite RANREPORT_RELEASE_DIR=<stage>/releases \
    python3 -m randreport.web build --periods 2025-08
{"published": [["2025-08", "rel_e20401d49406a66e"]]}
exit=0

# 7) страницы релиза без приватной БД
$ python3 -m randreport.web check --release-dir <stage>/releases
{"pages_checked": 120, "problems": [], "problem_count": 0}
exit=0
```

## 5. Границы проверки

* Проверены только код, синтетика и командная строка. Живые источники,
  systemd-службы, HTTPS и внешняя публикация не затрагивались (они в карточке
  `t_a831193f`).
* Ночной цикл (`randreport.ops.cli run`) намеренно **не** менялся: публикация
  там по-прежнему управляется `--publish-dir`/`RANREPORT_PUBLISH_DIR`, и отказ
  при его отсутствии не вводился, чтобы не ломать синхронизацию приватного
  стора. Отсутствие каталога публикации в ночном прогоне видно оператору по
  пустому `releases` в отчёте цикла.
* `web correct`/`seed` не менялись: у них `--db` объявлен обязательным в
  argparse, и отказ уже происходил до кода.
