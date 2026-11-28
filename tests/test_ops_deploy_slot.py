"""Единственный слот выкладки и автоматический откат (карточка t_f358548a).

Здесь запускается **настоящий** ``deploy/randreport-deploy.sh`` в синтетической
площадке: источник — клон этого репозитория (ветка main), приёмник — пустой
каталог со «своим» ``.venv``, ``systemctl`` и ``curl`` — подделки в ``PATH``.
Поэтому воспроизводится именно тот класс дефекта, который закрывает карточка:

* две выкладки одновременно — вторая ждёт слот и не перезаписывает дерево;
* служба без перезапуска / не поднялась — выкладка не объявляется успешной;
* живой адрес отдаёт байты, которых нет в выложенном дереве — успех не
  объявляется (даже при 200), дерево откатывается из архива;
* дым не прошёл (500) — автоматический откат к прежнему дереву и явный сигнал.

Ничего внешнего не читается: релиз для ``web check`` собирается синтетическим
``tests.dev_release``, сети нет, приватная БД не используется.
"""

from __future__ import annotations

import grp
import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from tests import dev_release

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "randreport-deploy.sh"

#: Подделка systemctl: restart можно замедлить (чтобы окно слота было измеримым),
#: состояние службы и коды выхода задаются переменными.
SYSTEMCTL_FAKE = """#!/usr/bin/env bash
set -u
command=${1:-}
case "$command" in
  restart)
    delay=${FAKE_RESTART_DELAY:-0}
    counter=${FAKE_RESTART_FAIL_FILE:-/dev/null}
    fail_times=${FAKE_RESTART_FAIL_TIMES:-0}
    if [ "$fail_times" != "0" ] && [ "$counter" != "/dev/null" ] && [ -f "$counter" ]; then
      n=$(cat "$counter" 2>/dev/null || echo 0)
      n=$((n + 1))
      echo "$n" >"$counter"
      if [ "$n" -le "$fail_times" ]; then
        echo "fake systemctl: restart failed" >&2
        exit 1
      fi
    fi
    sleep "$delay"
    printf 'restart %s\\n' "$*" >>"${FAKE_RESTART_LOG:-/dev/null}"
    exit 0 ;;
  is-active)
    unit=""
    for arg in "$@"; do
      case "$arg" in -*) ;; *) unit=${arg%.service} ;; esac
    done
    active="${FAKE_ACTIVE_UNITS:-randreport-public randreport-admin}"
    case " $active " in
      *" $unit "*) echo "active" ;;
      *) echo "inactive" ;;
    esac
    exit 0 ;;
  show)
    prop=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -p) prop=${2:-}; shift ;;
        --value) ;;
      esac
      shift
    done
    case "$prop" in
      ExecMainStatus) echo "${FAKE_EXEC_MAIN_STATUS:-0}" ;;
      Result) echo "${FAKE_UNIT_RESULT:-success}" ;;
      *) echo "" ;;
    esac
    exit 0 ;;
esac
exit 0
"""

#: Подделка curl: код ответа берётся по порядку из файла FAKE_CURL_CODES (последний
#: повторяется), содержимое /static/* — из FAKE_CURL_STATIC_ROOT (замороженная
#: копия дерева, то есть то, что отдаёт работающий процесс).
CURL_FAKE = """#!/usr/bin/env bash
set -u
url=""
fmt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift 2; continue ;;
    -w) fmt=${2:-}; shift 2; continue ;;
    --max-time) shift 2; continue ;;
    -*) shift; continue ;;
    *) url=$1; shift; continue ;;
  esac
done

if [ -n "$fmt" ]; then
  counter=${FAKE_CURL_CALLS:-/dev/null}
  n=0
  if [ -f "$counter" ]; then n=$(cat "$counter" 2>/dev/null || echo 0); fi
  n=$((n + 1))
  if [ "$counter" != "/dev/null" ]; then echo "$n" >"$counter"; fi
  code=200
  codes_file=${FAKE_CURL_CODES:-}
  if [ -n "$codes_file" ] && [ -f "$codes_file" ]; then
    mapfile -t codes <"$codes_file"
    if [ "${#codes[@]}" -gt 0 ]; then
      idx=$((n - 1))
      if [ "$idx" -ge "${#codes[@]}" ]; then idx=$((${#codes[@]} - 1)); fi
      code=${codes[$idx]}
    fi
  fi
  printf '%s' "$code"
  exit 0
fi

rel=${url#*://}
rel=${rel#*/}
rel=${rel#static/}
root=${FAKE_CURL_STATIC_ROOT:-}
if [ -n "$root" ] && [ -f "$root/randreport/web/static/$rel" ]; then
  cat "$root/randreport/web/static/$rel"
fi
exit 0
"""

SLOT_LINE = re.compile(r"^\[([0-9T:+\-]+)\] slot: (acquired|released)")


class DeployScriptTests(unittest.TestCase):
    """Скрипт выкладки целиком: слот, копирование, проверки, откат."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.release_base = Path(tempfile.mkdtemp(prefix="deploy-release-"))
        cls.release_dir = cls.release_base / "release"
        cls.release_dir.mkdir(parents=True, exist_ok=True)
        dev_release.build(str(cls.release_dir), period="2025-07", db_path=str(cls.release_base / "dev.sqlite"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.release_base, ignore_errors=True)

    # ------------------------------------------------------------------ setup
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="deploy-sandbox-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        (self.base / "tmp").mkdir()

        self.src = self.base / "src"
        self.git("clone", "--quiet", "--local", str(ROOT), str(self.src))
        # A clone of a linked worktree checks out that worktree's branch, while
        # the deploy policy and every scenario below are main-based; seat the
        # sandbox on origin/main (a no-op where the clone already defaulted to it).
        self.git("-C", str(self.src), "checkout", "-q", "-B", "main", "origin/main")
        self.git("-C", str(self.src), "config", "user.email", "deploy-tests@example.invalid")
        self.git("-C", str(self.src), "config", "user.name", "deploy tests")

        self.target = self.base / "opt" / "randreport"
        self.target.mkdir(parents=True)
        (self.target / ".venv").mkdir()
        (self.target / ".venv" / "sentinel").write_text("интерпретатор службы\n", encoding="utf-8")

        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.write_tool("systemctl", SYSTEMCTL_FAKE)
        self.write_tool("curl", CURL_FAKE)
        self.restart_log = self.base / "restarts.log"
        self.restart_fail_counter = self.base / "restart-failures"
        self.calls = self.base / "curl-calls"
        self.restart_log.write_text("", encoding="utf-8")
        self.restart_fail_counter.write_text("0", encoding="utf-8")
        self.calls.write_text("0", encoding="utf-8")
        self.restart_fail_times = "0"

    # ------------------------------------------------------ оружие площадки
    def arm_curl(self, codes: str) -> None:
        """Задать коды ответов живого адреса начиная со следующего вызова."""
        (self.base / "codes").write_text(codes, encoding="utf-8")
        self.calls.write_text("0", encoding="utf-8")

    def arm_restart_failures(self, times: int) -> None:
        """Сколько первых restart'ов подделка считает неудачными."""
        self.restart_fail_counter.write_text("0", encoding="utf-8")
        self.restart_fail_times = str(times)

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, f"git {' '.join(args)}: {proc.stderr}")
        return proc.stdout

    def write_tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def environment(self, report: Path | None = None, **overrides: object) -> dict[str, str]:
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(self.base)),
            "TMPDIR": str(self.base / "tmp"),
            "RANREPORT_DEPLOY_SOURCE": str(self.src),
            "RANREPORT_DEPLOY_TARGET": str(self.target),
            "RANREPORT_DEPLOY_LOCK": str(self.base / "deploy.lock"),
            "RANREPORT_DEPLOY_SLOT_WAIT": "90",
            "RANREPORT_DEPLOY_BACKUP_DIR": str(self.base / "backups"),
            "RANREPORT_DEPLOY_BACKUP_KEEP": "5",
            "RANREPORT_DEPLOY_UNITS": "randreport-public randreport-admin",
            "RANREPORT_DEPLOY_IDLE_UNITS": "randreport-sync.service",
            "RANREPORT_DEPLOY_OWNER": f"{user}:{group}",
            "RANREPORT_DEPLOY_RELEASE_DIR": str(self.release_dir),
            "RANREPORT_DEPLOY_PYTHON": sys.executable,
            "RANREPORT_DEPLOY_STATE": str(self.base / "state"),
            "RANREPORT_DEPLOY_SMOKE_BASE": "https://rand.example.invalid",
            "RANREPORT_DEPLOY_SMOKE_PATHS": "/ /static/approved-home.css",
            "RANREPORT_DEPLOY_SMOKE_ATTEMPTS": "2",
            "RANREPORT_DEPLOY_SMOKE_DELAY": "0",
            "RANREPORT_DEPLOY_SERVICE_ATTEMPTS": "2",
            "RANREPORT_DEPLOY_REPORT": str(report or (self.base / "report.txt")),
            "RANREPORT_DEPLOY_PRIV": "skip",
            "RANREPORT_DEPLOY_SYSTEMCTL": str(self.bin / "systemctl"),
            "RANREPORT_DEPLOY_CURL": str(self.bin / "curl"),
            "FAKE_RESTART_DELAY": "1",
            "FAKE_RESTART_LOG": str(self.restart_log),
            "FAKE_RESTART_FAIL_FILE": str(self.restart_fail_counter),
            "FAKE_RESTART_FAIL_TIMES": self.restart_fail_times,
            "FAKE_CURL_CALLS": str(self.calls),
            "FAKE_CURL_CODES": str(self.base / "codes"),
            "FAKE_CURL_STATIC_ROOT": str(self.target),
        }
        env.update({key: str(value) for key, value in overrides.items()})
        return env

    def run_script(self, *args: str, report: Path | None = None, timeout: int = 600, **overrides: object):
        env = self.environment(report=report, **overrides)
        proc = subprocess.run(
            [str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=timeout
        )
        report_path = Path(env["RANREPORT_DEPLOY_REPORT"])
        text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        return proc, text

    def spawn_script(self, *args: str, report: Path, **overrides: object):
        env = self.environment(report=report, **overrides)
        return subprocess.Popen(
            [str(SCRIPT), *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    # ------------------------------------------------------------- helpers
    def tracked_files(self) -> list[str]:
        return [line for line in self.git("-C", str(self.src), "ls-files").splitlines() if line]

    def tracked_dirs(self) -> list[str]:
        dirs = {"."}
        for rel in self.tracked_files():
            parts = rel.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:index]))
        return sorted(dirs)

    def exec_files(self) -> list[str]:
        out = self.git("-C", str(self.src), "ls-files", "-s")
        return [line.split("\t", 1)[1] for line in out.splitlines() if line.startswith("100755")]

    def digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def slot_span(self, report: str):
        start = end = None
        for line in report.splitlines():
            match = SLOT_LINE.match(line)
            if not match:
                continue
            stamp = datetime.fromisoformat(match.group(1))
            if match.group(2) == "acquired":
                start = stamp
            else:
                end = stamp
        return (start, end) if start and end else None

    def summary_line(self, report: str, prefix: str) -> str:
        for line in report.splitlines():
            if line.startswith(prefix):
                return line
        return ""

    # ----------------------------------------------------------------- слот
    def test_two_parallel_releases_cannot_interleave(self) -> None:
        report_a = self.base / "a.txt"
        report_b = self.base / "b.txt"
        first = self.spawn_script("deploy", report=report_a)
        second = self.spawn_script("deploy", report=report_b)
        out_a, _ = first.communicate(timeout=600)
        out_b, _ = second.communicate(timeout=600)
        text_a = report_a.read_text(encoding="utf-8")
        text_b = report_b.read_text(encoding="utf-8")
        self.assertEqual(first.returncode, 0, out_a)
        self.assertEqual(second.returncode, 0, out_b)

        spans = [span for span in (self.slot_span(text_a), self.slot_span(text_b)) if span]
        self.assertEqual(len(spans), 2, "каждая выкладка обязана показать границы слота")
        (start_1, end_1), (start_2, end_2) = sorted(spans)
        self.assertLessEqual(
            end_1, start_2, f"слоты двух выкладок пересеклись: {spans} — дерево писалось одновременно"
        )
        waiters = [name for name, text in (("a", text_a), ("b", text_b)) if "ждём слот" in text]
        self.assertTrue(waiters, "вторая выкладка вошла в слот, не подождав первую")

    def test_deploy_waits_for_a_held_slot_and_times_out_explicitly(self) -> None:
        lock = self.base / "deploy.lock"
        holder = subprocess.Popen(["flock", str(lock), "sleep", "4"])
        try:
            time.sleep(0.4)
            started = time.time()
            proc, report = self.run_script("deploy")
            waited = time.time() - started
        finally:
            holder.wait(timeout=60)
        self.assertEqual(proc.returncode, 0, report)
        self.assertIn("ждём слот", report)
        self.assertGreaterEqual(waited, 3.0, "выкладка не стала ждать занятый слот")

        holder = subprocess.Popen(["flock", str(lock), "sleep", "6"])
        try:
            time.sleep(0.4)
            proc, report = self.run_script("deploy", RANREPORT_DEPLOY_SLOT_WAIT="1")
        finally:
            holder.wait(timeout=60)
        self.assertEqual(proc.returncode, 3, report)
        self.assertIn("НЕ ДОЖДАЛИСЬ слота", report)
        self.assertNotIn("копирование: rsync", report, "выкладка тронула приёмник, не дождавшись слота")

    # --------------------------------------------------------- копирование
    def test_deploy_keeps_venv_cleans_stale_files_and_sets_modes(self) -> None:
        stale = self.target / "stale-from-an-old-release.txt"
        stale.write_text("мусор прошлой выкладки\n", encoding="utf-8")
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)

        self.assertTrue(
            (self.target / ".venv" / "sentinel").is_file(),
            ".venv службы снесён выкладкой (именно это дало 203/EXEC)",
        )
        self.assertFalse(stale.exists(), "--delete не убрал посторонний файл в приёмнике")

        for rel in self.tracked_files():
            self.assertEqual(
                self.digest(self.src / rel),
                self.digest(self.target / rel),
                f"{rel} в приёмнике не совпадает с коммитом",
            )

        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        exec_set = set(self.exec_files())
        for rel in self.tracked_dirs():
            path = self.target if rel == "." else self.target / rel
            info = os.stat(path)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o750, f"каталог {rel} не 0750")
            self.assertEqual(f"{pwd.getpwuid(info.st_uid).pw_name}:{grp.getgrgid(info.st_gid).gr_name}", f"{user}:{group}")
        for rel in self.tracked_files():
            info = os.stat(self.target / rel)
            want = 0o750 if rel in exec_set else 0o640
            self.assertEqual(stat.S_IMODE(info.st_mode), want, f"{rel}: ожидался {oct(want)}")
        self.assertTrue(exec_set, "в репозитории должны быть исполняемые файлы (deploy/*.sh)")

        self.assertIn(".venv", report)
        self.assertTrue(list((self.base / "backups").glob("opt-randreport-pre-deploy-*.tgz")), "нет архива отката")

    # ------------------------------------------------------------ проверки
    def test_verify_runs_manifest_web_check_and_smoke_in_one_command(self) -> None:
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)

        proc, report = self.run_script("verify")
        self.assertEqual(proc.returncode, 0, report)
        self.assertRegex(report, r"манифест:\s+\d+/\d+ OK")
        self.assertRegex(report, r"web check:\s+страниц \d+, проблем 0")
        self.assertIn("/=200", report)
        self.assertIn("/static/approved-home.css=200", report)
        self.assertIn("ИТОГ: ПРОВЕРКИ ПРОЙДЕНЫ", report)

    def test_verify_reports_a_tree_that_drifted_from_the_manifest(self) -> None:
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)
        drifted = self.target / "randreport" / "web" / "render.py"
        drifted.write_text(drifted.read_text(encoding="utf-8") + "\n# ручная правка на сервере\n", encoding="utf-8")

        proc, report = self.run_script("verify")
        self.assertEqual(proc.returncode, 1, report)
        self.assertIn("НЕ СОВПАЛ", report)
        self.assertIn("ИТОГ: ПРОВЕРКИ НЕ ПРОЙДЕНЫ", report)

    def test_verify_checks_the_deployed_commit_and_reports_a_source_that_moved_ahead(self) -> None:
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)
        progress = self.src / "docs" / "PROGRESS.md"
        progress.write_text(progress.read_text(encoding="utf-8") + "\nследующий выпуск\n", encoding="utf-8")
        self.git("-C", str(self.src), "commit", "-aqm", "next release")

        proc, report = self.run_script("verify")
        self.assertEqual(proc.returncode, 0, report)
        self.assertIn("main ушёл вперёд", report)
        self.assertIn("ИТОГ: ПРОВЕРКИ ПРОЙДЕНЫ", report)

    # --------------------------------------------------------------- откат
    def test_failed_smoke_rolls_back_and_leaves_the_site_up(self) -> None:
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)
        render = self.target / "randreport" / "web" / "render.py"
        before = render.read_bytes()

        (self.src / "randreport" / "web" / "render.py").write_bytes(
            before + "\n# следующий выпуск\n".encode("utf-8")
        )
        self.git("-C", str(self.src), "commit", "-aqm", "next release")
        self.arm_curl("500\n500\n200\n200\n")

        proc, report = self.run_script("deploy", RANREPORT_DEPLOY_SMOKE_PATHS="/")
        self.assertEqual(proc.returncode, 4, report)
        self.assertIn("ПРОВАЛ", report)
        self.assertIn("ОТКАЧЕН", report)
        self.assertEqual(render.read_bytes(), before, "откат не вернул прежнее дерево")
        self.assertRegex(report, r"дым после отката: /=200")
        self.assertRegex(self.summary_line(report, "ИТОГ: ВЫКЛАДКА ПРОВАЛЕНА"), r"сайт ОТКАЧЕН")
        restarts = self.restart_log.read_text(encoding="utf-8").count("restart")
        self.assertGreaterEqual(restarts, 2, "после отката службы обязаны быть перезапущены")

    def test_live_bytes_that_differ_from_the_deployed_tree_fail_the_release(self) -> None:
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)
        live = self.base / "live"
        shutil.copytree(self.target, live)  # то, что отдаёт работающий процесс
        css = self.target / "randreport" / "web" / "static" / "approved-home.css"
        before = css.read_bytes()

        (self.src / "randreport" / "web" / "static" / "approved-home.css").write_bytes(
            before + "\n/* следующий выпуск */\n".encode("utf-8")
        )
        self.git("-C", str(self.src), "commit", "-aqm", "next css")

        proc, report = self.run_script("deploy", FAKE_CURL_STATIC_ROOT=str(live))
        self.assertEqual(proc.returncode, 4, report)
        self.assertIn("ОТЛИЧАЕТСЯ", report)
        self.assertIn("ОТКАЧЕН", report)
        self.assertEqual(css.read_bytes(), before, "откат не вернул прежний файл статики")

    def test_restart_failure_rolls_back_and_leaves_the_site_up(self) -> None:
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)
        render = self.target / "randreport" / "web" / "render.py"
        before = render.read_bytes()
        (self.src / "randreport" / "web" / "render.py").write_bytes(
            before + "\n# выпуск, который не перезапустился\n".encode("utf-8")
        )
        self.git("-C", str(self.src), "commit", "-aqm", "release that will not restart")
        self.arm_restart_failures(1)

        proc, report = self.run_script("deploy", RANREPORT_DEPLOY_SMOKE_PATHS="/")
        self.assertEqual(proc.returncode, 4, report)
        self.assertIn("restart завершился ошибкой", report)
        self.assertIn("ОТКАЧЕН", report)
        self.assertEqual(render.read_bytes(), before, "откат не вернул прежнее дерево")
        self.assertRegex(report, r"дым после отката: /=200")

    def test_units_that_stay_broken_are_an_explicit_signal(self) -> None:
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 0, report)
        render = self.target / "randreport" / "web" / "render.py"
        before = render.read_bytes()
        (self.src / "randreport" / "web" / "render.py").write_bytes(
            before + "\n# выпуск со сломанной службой\n".encode("utf-8")
        )
        self.git("-C", str(self.src), "commit", "-aqm", "release with a broken unit")

        proc, report = self.run_script("deploy", FAKE_EXEC_MAIN_STATUS="1")
        self.assertEqual(proc.returncode, 5, report)
        self.assertIn("службы не поднялись", report)
        self.assertIn("ОТКАТ НЕ УДАЛСЯ", report)
        self.assertIn("нужен человек", report)
        self.assertEqual(render.read_bytes(), before, "дерево всё же вернулось из архива")

    # ---------------------------------------------------------- отказы рано
    def test_refuses_a_dirty_source_and_a_non_main_ref(self) -> None:
        dirty = self.src / "randreport" / "web" / "render.py"
        dirty.write_text(dirty.read_text(encoding="utf-8") + "\n# правка без коммита\n", encoding="utf-8")
        proc, report = self.run_script("deploy")
        self.assertEqual(proc.returncode, 2, report)
        self.assertIn("незакоммиченные правки", report)
        self.assertFalse((self.target / "randreport").exists(), "приёмник тронут до проверок источника")
        self.assertFalse((self.base / "backups").exists(), "резерв снят до проверок источника")

        self.git("-C", str(self.src), "checkout", "-q", "--", ".")
        self.git("-C", str(self.src), "checkout", "-qb", "revision/other")
        proc, report = self.run_script("deploy", RANREPORT_DEPLOY_REF="revision/other")
        self.assertEqual(proc.returncode, 2, report)
        self.assertIn("не main", report)
        self.assertFalse((self.target / "randreport").exists())

    # ------------------------------------------------------ сам скрипт в kit
    def test_deploy_script_ships_executable_with_the_required_gates(self) -> None:
        self.assertTrue(SCRIPT.is_file(), "нет deploy/randreport-deploy.sh")
        self.assertTrue(os.access(SCRIPT, os.X_OK), "скрипт выкладки должен быть исполняемым")
        text = SCRIPT.read_text(encoding="utf-8")
        for needle in (
            "flock",
            "--delete",
            "--exclude=.venv",
            "sha256sum -c",
            "randreport.web check",
            "systemctl",
            "is-active",
            "ExecMainStatus",
            "Result",
            "/static/approved-home.css",
            "opt-randreport-pre-deploy-",
        ):
            self.assertIn(needle, text, f"в скрипте выкладки нет «{needle}»")
        # дефолтная дымовая проверка карточки: живой адрес и статика
        self.assertIn('RANREPORT_DEPLOY_SMOKE_PATHS:-"/ /static/approved-home.css"', text)
        self.assertIn("https://rand.185.121.13.113.sslip.io", text)
