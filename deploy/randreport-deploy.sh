#!/usr/bin/env bash
# Единственная дверь выкладки кода в /opt/randreport (карточка t_f358548a).
#
# Зачем. Две релизные карточки один раз писали в /opt/randreport одновременно:
# код заменился под работающим процессом, служба не перезапустилась, живой сайт
# отдавал 500 на всех маршрутах (включая /static/*), а /opt/randreport/.venv был
# снесён (randreport-public.service не запускался: 203/EXEC). Восстановлено вручную.
#
# Что гарантирует скрипт:
#   * ОДИН слот выкладки (flock на $LOCK): вторая выкладка ЖДЁТ окончания первой
#     и не перезаписывает дерево; не дождавшись за $SLOT_WAIT секунд — выходит с
#     кодом 3 и явной строкой в отчёте (никогда не «тихо перезаписывает»).
#   * источник — ровно один коммит `main` (или --ref/REF), только отслеживаемые
#     файлы: `git archive` уходит в rsync-приёмник, поэтому в /opt/randreport не
#     попадают ни .git, ни кэши, ни посторонние файлы;
#   * `rsync --delete` с БЕЗУСЛОВНЫМ исключением `.venv` (интерпретатор службы
#     нельзя снести: он не в git, но он нужен службе);
#   * владелец root:randreport, каталоги 0750, файлы 0640, исполняемые (git-режим
#     100755) 0750;
#   * обязательный `systemctl restart` СРАЗУ после копирования, с проверкой
#     is-active / ExecMainStatus / Result по каждой службе;
#   * `sha256sum -c` манифеста (пути — из git, хэши — из коммита), `web check`
#     (0 проблем) и дымовая проверка живого HTTPS (200 на `/` и
#     `/static/approved-home.css`) — те же проверки одной командой в `verify`;
#   * любая неудача после копирования → автоматический откат из архива
#     /var/backups/randreport/opt-randreport-pre-deploy-*.tgz, перезапуск служб,
#     повторный дым и ЯВНЫЙ сигнал (код 4; 5 — если не откатилось).
#
# Использование:
#   randreport-deploy.sh deploy             выложить main (под слотом)
#   randreport-deploy.sh verify             проверки п.3+п.4 одной командой
#   randreport-deploy.sh rollback [ARCHIVE] откат из архива (по умолчанию — последний)
#   randreport-deploy.sh slot CMD [ARGS…]   выполнить команду под слотом выкладки
#
# Настройки (переменные окружения, значения по умолчанию — боевые):
#   RANREPORT_DEPLOY_SOURCE=/home/agent/projects/random-report  источник (репозиторий)
#   RANREPORT_DEPLOY_REF=main                 выкладываемый ref (не main — только с ALLOW_REF=1)
#   RANREPORT_DEPLOY_TARGET=/opt/randreport   приёмник
#   RANREPORT_DEPLOY_LOCK=/var/lock/randreport-deploy.lock   файл слота
#   RANREPORT_DEPLOY_SLOT_WAIT=900            сколько секунд ждать слот
#   RANREPORT_DEPLOY_BACKUP_DIR=/var/backups/randreport
#   RANREPORT_DEPLOY_BACKUP_KEEP=10           сколько архивов выкладки хранить
#   RANREPORT_DEPLOY_UNITS="randreport-public randreport-admin"
#   RANREPORT_DEPLOY_IDLE_UNITS=randreport-sync.service   дождаться, если идёт ночной сбор
#   RANREPORT_DEPLOY_OWNER=root:randreport
#   RANREPORT_DEPLOY_RELEASE_DIR=/var/www/randreport
#   RANREPORT_DEPLOY_PYTHON=<$TARGET/.venv/bin/python>  интерпретатор для web check
#   RANREPORT_DEPLOY_STATE=/var/lib/randreport/deploy   deployed.json + manifest.sha256
#   RANREPORT_DEPLOY_SMOKE_BASE=https://rand.185.121.13.113.sslip.io
#   RANREPORT_DEPLOY_SMOKE_PATHS="/ /static/approved-home.css"
#   RANREPORT_DEPLOY_SMOKE_ATTEMPTS=15 / _DELAY=2
#   RANREPORT_DEPLOY_REPORT=/path/report.txt  дополнительно писать отчёт в файл
#   RANREPORT_DEPLOY_PRIV=…                   префикс привилегий ('' для root, 'sudo -n'; 'skip' — тесты)
#
# Коды возврата: 0 — успех; 2 — отказ до копирования (источник/аргументы);
# 3 — не дождались слота; 4 — неудача после копирования, дерево откачено;
# 5 — неудача после копирования и откат не удался (сайт может быть нездоров).

set -euo pipefail

PROG=${0##*/}

SOURCE=${RANREPORT_DEPLOY_SOURCE:-/home/agent/projects/random-report}
REF=${RANREPORT_DEPLOY_REF:-main}
ALLOW_REF=${RANREPORT_DEPLOY_ALLOW_REF:-0}
TARGET=${RANREPORT_DEPLOY_TARGET:-/opt/randreport}
LOCK=${RANREPORT_DEPLOY_LOCK:-/var/lock/randreport-deploy.lock}
SLOT_WAIT=${RANREPORT_DEPLOY_SLOT_WAIT:-900}
BACKUP_DIR=${RANREPORT_DEPLOY_BACKUP_DIR:-/var/backups/randreport}
BACKUP_KEEP=${RANREPORT_DEPLOY_BACKUP_KEEP:-10}
UNITS=${RANREPORT_DEPLOY_UNITS:-"randreport-public randreport-admin"}
IDLE_UNITS=${RANREPORT_DEPLOY_IDLE_UNITS:-randreport-sync.service}
OWNER=${RANREPORT_DEPLOY_OWNER:-root:randreport}
RELEASE_DIR=${RANREPORT_DEPLOY_RELEASE_DIR:-/var/www/randreport}
STATE=${RANREPORT_DEPLOY_STATE:-/var/lib/randreport-deploy}
SMOKE_BASE=${RANREPORT_DEPLOY_SMOKE_BASE:-https://rand.185.121.13.113.sslip.io}
SMOKE_PATHS=${RANREPORT_DEPLOY_SMOKE_PATHS:-"/ /static/approved-home.css"}
SMOKE_ATTEMPTS=${RANREPORT_DEPLOY_SMOKE_ATTEMPTS:-15}
SMOKE_DELAY=${RANREPORT_DEPLOY_SMOKE_DELAY:-2}
SERVICE_ATTEMPTS=${RANREPORT_DEPLOY_SERVICE_ATTEMPTS:-10}
SERVICE_USER=${RANREPORT_DEPLOY_SERVICE_USER:-randreport}
SYSTEMCTL_BIN=${RANREPORT_DEPLOY_SYSTEMCTL:-systemctl}
CURL_BIN=${RANREPORT_DEPLOY_CURL:-curl}
REPORT_FILE=${RANREPORT_DEPLOY_REPORT:-}

if [ -n "${RANREPORT_DEPLOY_PYTHON:-}" ]; then
  PYTHON=$RANREPORT_DEPLOY_PYTHON
elif [ -x "$TARGET/.venv/bin/python" ]; then
  PYTHON=$TARGET/.venv/bin/python
else
  PYTHON=python3
fi

# Привилегированный префикс. Боевой запуск — от root (пусто); непривилегированный —
# sudo -n; 'skip' — тестовый режим (всё делается текущим пользователем).
if [ -n "${RANREPORT_DEPLOY_PRIV:-}" ]; then
  case "$RANREPORT_DEPLOY_PRIV" in
    skip) PRIV_PREFIX="" ;;
    *) PRIV_PREFIX=$RANREPORT_DEPLOY_PRIV ;;
  esac
elif [ "$(id -u)" -eq 0 ]; then
  PRIV_PREFIX=""
else
  PRIV_PREFIX="sudo -n"
fi

OWNER_USER=${OWNER%%:*}
OWNER_GROUP=${OWNER##*:}

COMMIT=""
STAGE=""
MANIFEST=""
WORK=""
ARCHIVE=""
FILE_COUNT=0
DIR_COUNT=0
EXEC_COUNT=0
MANIFEST_OK=0
PAGES=0
PROBLEMS=-1
SMOKE_SUMMARY="не выполнялся"
RESTART_LOG=""
SLOT_ACQUIRED=0
ACTUALITY=""
FINAL=""

# ---------------------------------------------------------------- reporting --
if [ -n "$REPORT_FILE" ]; then
  mkdir -p "$(dirname "$REPORT_FILE")" 2>/dev/null || true
  : >"$REPORT_FILE"
fi

emit() {
  printf '%s\n' "$*"
  [ -n "$REPORT_FILE" ] && printf '%s\n' "$*" >>"$REPORT_FILE"
  return 0
}

log() {
  emit "[$(date -Is)] $*"
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [ -n "$WORK" ] && [ -d "$WORK" ]; then rm -rf "$WORK" || true; fi
  if [ "$SLOT_ACQUIRED" -eq 1 ]; then
    SLOT_ACQUIRED=0
    log "slot: released pid=$$"
  fi
  exit "$rc"
}

die() {  # die КОД СООБЩЕНИЕ…
  local code=$1; shift
  log "отказ: $*"
  exit "$code"
}

# ------------------------------------------------------------------- helpers --
run_priv() {
  if [ -n "$PRIV_PREFIX" ]; then
    # shellcheck disable=SC2086
    $PRIV_PREFIX "$@"
  else
    "$@"
  fi
}

run_as_service() {
  if [ "$(id -u)" -eq 0 ] || [ -n "$PRIV_PREFIX" ]; then
    run_priv setpriv --reuid="$SERVICE_USER" --regid="$SERVICE_USER" --init-groups "$@"
  else
    "$@"
  fi
}

sctl() {
  if [ -n "$PRIV_PREFIX" ]; then
    # shellcheck disable=SC2086
    $PRIV_PREFIX "$SYSTEMCTL_BIN" "$@"
  else
    "$SYSTEMCTL_BIN" "$@"
  fi
}

list_paths()  { git -C "$SOURCE" ls-tree -r --name-only "$1"; }

list_exec_paths() {
  git -C "$SOURCE" ls-tree -r "$1" | awk -F'\t' '$1 ~ /^100755 /{print $2}'
}

list_dirs() {
  {
    printf '.\n'
    list_paths "$1" | awk -F/ 'NF > 1 { p = ""; for (i = 1; i < NF; i++) { p = (i == 1 ? $i : p "/" $i); print p } }'
  } | sort -u
}

# --------------------------------------------------------------------- слот --
acquire_slot() {  # $1 = x (эксклюзивно, по умолчанию) | s (разделяемо)
  local mode=${1:-x} waited=0 flag=-x
  [ "$mode" = s ] && flag=-s
  exec 9>"$LOCK" || die 2 "не открыть файл слота $LOCK"
  if [ "$SLOT_WAIT" -le 0 ]; then
    flock $flag 9 || die 3 "не удалось встать в слот выкладки ($LOCK)"
  else
    while ! flock -n $flag 9; do
      if [ "$waited" -eq 0 ]; then
        log "slot: ждём слот выкладки — $LOCK держит другая выкладка (ждём до ${SLOT_WAIT}s, ничего не перезаписываем)"
      fi
      sleep 1
      waited=$((waited + 1))
      if [ "$waited" -ge "$SLOT_WAIT" ]; then
        log "slot: НЕ ДОЖДАЛИСЬ слота за ${SLOT_WAIT}s — выкладка не начата, дерево не тронуто"
        exit 3
      fi
      if [ $((waited % 30)) -eq 0 ]; then
        log "slot: всё ещё ждём слот (${waited}s)"
      fi
    done
  fi
  SLOT_ACQUIRED=1
  log "slot: acquired pid=$$ lock=$LOCK mode=$mode"
}

# ---------------------------------------------------------------- preflight --
preflight() {
  command -v git >/dev/null || die 2 "нет git"
  command -v rsync >/dev/null || die 2 "нет rsync"
  [ -d "$SOURCE/.git" ] || die 2 "источник не git-репозиторий: $SOURCE"
  git -C "$SOURCE" rev-parse --verify --quiet "$REF^{commit}" >/dev/null \
    || die 2 "в $SOURCE нет ref '$REF'"
  if [ "$REF" != "main" ] && [ "$ALLOW_REF" != "1" ]; then
    die 2 "ref '$REF' не main: единственный штатный источник выкладки — main (RANREPORT_DEPLOY_ALLOW_REF=1 снимает запрет)"
  fi
  local dirty
  dirty=$(git -C "$SOURCE" status --porcelain --untracked-files=no)
  [ -z "$dirty" ] || die 2 "в источнике есть незакоммиченные правки отслеживаемых файлов: $(printf '%s' "$dirty" | tr '\n' ' ')"
  COMMIT=$(git -C "$SOURCE" rev-parse "$REF^{commit}")
  local head_ref
  head_ref=$(git -C "$SOURCE" symbolic-ref -q --short HEAD || echo "detached")
  log "источник: $SOURCE $REF @ $(git -C "$SOURCE" rev-parse --short "$COMMIT") (HEAD: $head_ref, дерево чистое)"
  [ "$TARGET" != "$SOURCE" ] || die 2 "приёмник совпадает с источником"
  [ -d "$TARGET" ] || die 2 "приёмник не существует: $TARGET"
  if [ ! -x "$TARGET/.venv/bin/python" ]; then
    log "ВНИМАНИЕ: $TARGET/.venv/bin/python отсутствует — службы поднимаются интерпретатором из .venv, проверьте его отдельно"
  fi
}

# Ночной сбор читает код из /opt/randreport: не меняем дерево под его процессом.
wait_for_idle_units() {
  local unit state waited=0
  for unit in $IDLE_UNITS; do
    state=$(sctl is-active "$unit" 2>/dev/null || true)
    [ "$state" = active ] || continue
    log "ожидание: $unit активна (идёт сбор) — выкладка не начинается под работающим процессом"
    while [ "$(sctl is-active "$unit" 2>/dev/null || true)" = active ]; do
      sleep 2
      waited=$((waited + 2))
      if [ "$SLOT_WAIT" -gt 0 ] && [ "$waited" -ge "$SLOT_WAIT" ]; then
        die 3 "$unit всё ещё активна через ${waited}s — выкладка не начата"
      fi
    done
    log "ожидание: $unit завершилась (${waited}s)"
  done
}

# -------------------------------------------------------------------- staging --
prepare_stage() {
  WORK=$(mktemp -d "${TMPDIR:-/var/tmp}/randreport-deploy.XXXXXX")
  STAGE=$WORK/tree
  MANIFEST=$WORK/manifest.sha256
  mkdir -p "$STAGE"
  git -C "$SOURCE" archive --format=tar "$COMMIT" | tar -x -C "$STAGE" \
    || die 2 "не удалось распаковать коммит $COMMIT"
  FILE_COUNT=$(list_paths "$COMMIT" | wc -l | tr -d ' ')
  EXEC_COUNT=$(list_exec_paths "$COMMIT" | wc -l | tr -d ' ')
  DIR_COUNT=$(list_dirs "$COMMIT" | wc -l | tr -d ' ')
  # Манифест: список путей — из git (тот же набор, что `git ls-files` в чистом
  # дереве на этом коммите), хэши — с распакованного коммита. Порядок фиксирован.
  ( cd "$STAGE" && list_paths "$COMMIT" | sort | xargs -d '\n' -r sha256sum ) >"$MANIFEST" \
    || die 2 "не удалось собрать манифест"
  local manifest_lines ls_files
  manifest_lines=$(wc -l <"$MANIFEST" | tr -d ' ')
  ls_files=$(git -C "$SOURCE" ls-files | wc -l | tr -d ' ')
  [ "$manifest_lines" = "$FILE_COUNT" ] || die 2 "манифест покрывает $manifest_lines из $FILE_COUNT файлов"
  log "подготовка: коммит $COMMIT, файлов $FILE_COUNT (манифест $manifest_lines, git ls-files в дереве: $ls_files), каталогов $DIR_COUNT, исполняемых $EXEC_COUNT"
}

# --------------------------------------------------------------------- backup --
backup_target() {
  local stamp short
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  short=$(git -C "$SOURCE" rev-parse --short "$COMMIT")
  ARCHIVE=$BACKUP_DIR/opt-randreport-pre-deploy-$stamp-$short.tgz
  run_priv mkdir -p "$BACKUP_DIR" || return 1
  run_priv tar czf "$ARCHIVE" -C "$(dirname "$TARGET")" "$(basename "$TARGET")" || return 1
  run_priv chmod 0640 "$ARCHIVE" || true
  log "резерв: $ARCHIVE ($(du -h "$ARCHIVE" 2>/dev/null | cut -f1)) — из него выполняется откат"
  prune_archives
}

prune_archives() {
  local old
  [ "$BACKUP_KEEP" -gt 0 ] || return 0
  old=$(ls -1t "$BACKUP_DIR"/opt-randreport-pre-deploy-*.tgz 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) || true)
  [ -n "$old" ] || return 0
  printf '%s\n' "$old" | while IFS= read -r f; do
    log "резерв: убираем старый архив $(basename "$f") (храним $BACKUP_KEEP)"
    run_priv rm -f "$f" || true
  done
}

# ------------------------------------------------------------------- копирование --
copy_tree() {
  local rsync_cmd=(rsync -a --delete --exclude=.venv --chmod=D750,F640)
  [ -n "$OWNER" ] && rsync_cmd+=(--chown="$OWNER")
  log "копирование: rsync --delete (исключён .venv) $STAGE/ -> $TARGET/"
  # shellcheck disable=SC2086
  $PRIV_PREFIX "${rsync_cmd[@]}" "$STAGE/" "$TARGET/" || return 1
  apply_modes || return 1
}

apply_modes() {
  # Владелец и режимы — только по путям из git: .venv и чужое дерево не трогаем.
  ( cd "$TARGET" && list_dirs "$COMMIT" | xargs -d '\n' -r ${PRIV_PREFIX} chown "$OWNER" ) || return 1
  ( cd "$TARGET" && list_paths "$COMMIT" | xargs -d '\n' -r ${PRIV_PREFIX} chown "$OWNER" ) || return 1
  ( cd "$TARGET" && list_dirs "$COMMIT" | xargs -d '\n' -r ${PRIV_PREFIX} chmod 0750 ) || return 1
  ( cd "$TARGET" && list_paths "$COMMIT" | xargs -d '\n' -r ${PRIV_PREFIX} chmod 0640 ) || return 1
  if [ "$EXEC_COUNT" -gt 0 ]; then
    ( cd "$TARGET" && list_exec_paths "$COMMIT" | xargs -d '\n' -r ${PRIV_PREFIX} chmod 0750 ) || return 1
  fi
  log "режимы: каталогов $DIR_COUNT → 0750, файлов $FILE_COUNT → 0640, исполняемых $EXEC_COUNT → 0750, владелец $OWNER"
}

# ------------------------------------------------------------------- проверки --
check_manifest() {
  local out ok total
  if [ ! -s "$MANIFEST" ]; then
    # standalone verify: манифест прошлой выкладки из STATE, иначе собираем из ref
    if [ -s "$STATE/manifest.sha256" ]; then
      MANIFEST=$STATE/manifest.sha256
    else
      log "манифест: в $STATE нет manifest.sha256 — собираем из $REF"
      prepare_stage >/dev/null
      MANIFEST=$WORK/manifest.sha256
    fi
  fi
  out=$( cd "$TARGET" && sha256sum -c "$MANIFEST" 2>&1 ) || true
  ok=$(printf '%s\n' "$out" | grep -c ': OK$' || true)
  total=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
  MANIFEST_OK=$ok
  if [ "$ok" = "$total" ] && [ "$total" -gt 0 ]; then
    log "манифест: $ok/$total OK (sha256sum -c из git-списка)"
    return 0
  fi
  log "манифест: НЕ СОВПАЛ ($ok/$total OK)"
  printf '%s\n' "$out" | grep -v ': OK$' | head -20 | while IFS= read -r line; do log "  $line"; done
  return 1
}

check_modes() {
  local bad=0 total=0 rel who mode path
  declare -A exec_set=()
  while IFS= read -r rel; do [ -n "$rel" ] && exec_set["$rel"]=1; done < <(list_exec_paths "$COMMIT")

  while read -r who mode path; do
    total=$((total + 1))
    [ "$who $mode" = "$OWNER_USER:$OWNER_GROUP 750" ] || {
      log "режимы: каталог $path — '$who $mode', ожидается '$OWNER_USER:$OWNER_GROUP 750'"
      bad=$((bad + 1))
    }
  done < <( cd "$TARGET" && list_dirs "$COMMIT" | xargs -d '\n' -r stat -c '%U:%G %a %n' 2>/dev/null || true )

  while read -r who mode path; do
    total=$((total + 1))
    local want=640
    [ -n "${exec_set[$path]:-}" ] && want=750
    [ "$who $mode" = "$OWNER_USER:$OWNER_GROUP $want" ] || {
      log "режимы: файл $path — '$who $mode', ожидается '$OWNER_USER:$OWNER_GROUP $want'"
      bad=$((bad + 1))
    }
  done < <( cd "$TARGET" && list_paths "$COMMIT" | xargs -d '\n' -r stat -c '%U:%G %a %n' 2>/dev/null || true )

  [ "$bad" -eq 0 ] || return 1
  log "режимы: проверено $total записей — все $OWNER_USER:$OWNER_GROUP 0750/0640 (исполняемые 0750)"
  return 0
}

check_web() {
  local out plain
  out=$( cd "$TARGET" && export RANREPORT_RELEASE_DIR="$RELEASE_DIR" && \
         run_as_service "$PYTHON" -m randreport.web check --db /nonexistent 2>&1 ) || {
    log "web check: команда завершилась ошибкой"
    printf '%s\n' "$out" | tail -20 | while IFS= read -r line; do log "  $line"; done
    return 1
  }
  plain=$(printf '%s\n' "$out" | tail -1)
  PAGES=$(printf '%s' "$plain" | sed -n 's/.*"pages_checked": *\([0-9]*\).*/\1/p')
  PROBLEMS=$(printf '%s' "$plain" | sed -n 's/.*"problem_count": *\([0-9]*\).*/\1/p')
  [ -n "$PAGES" ] || PAGES=0
  [ -n "$PROBLEMS" ] || PROBLEMS=-1
  log "web check: страниц $PAGES, проблем $PROBLEMS ($plain)"
  [ "$PROBLEMS" = "0" ]
}

smoke() {
  local path code attempt ok=1 summary="" live_hash local_hash local_file rel
  for path in $SMOKE_PATHS; do
    code=""
    attempt=0
    while [ "$attempt" -lt "$SMOKE_ATTEMPTS" ]; do
      attempt=$((attempt + 1))
      code=$("$CURL_BIN" -sS -o /dev/null -w '%{http_code}' --max-time 10 "${SMOKE_BASE}${path}" 2>/dev/null || true)
      [ "$code" = "200" ] && break
      [ "$attempt" -lt "$SMOKE_ATTEMPTS" ] && sleep "$SMOKE_DELAY"
    done
    [ -n "$summary" ] && summary="$summary, "
    summary="${summary}${path}=${code:-нет ответа}"
    log "дым: ${SMOKE_BASE}${path} -> ${code:-нет ответа} (попыток $attempt)"
    if [ "$code" != "200" ]; then
      ok=0
      continue
    fi
    # Для /static/* сверяем отдачу живого адреса с байтами выложенного дерева:
    # 200 сам по себе не доказывает, что сайт отдаёт именно выпущенный код.
    case "$path" in
      /static/*)
        rel=${path#/static/}
        local_file=$TARGET/randreport/web/static/$rel
        if [ -f "$local_file" ]; then
          local_hash=$(sha256sum "$local_file" | cut -d' ' -f1)
          live_hash=$("$CURL_BIN" -sS --max-time 10 "${SMOKE_BASE}${path}" 2>/dev/null | sha256sum | cut -d' ' -f1)
          if [ "$local_hash" = "$live_hash" ]; then
            log "дым: $path совпадает с выложенным файлом (sha256 ${local_hash:0:12}…)"
          else
            log "дым: $path ОТЛИЧАЕТСЯ от выложенного файла (live ${live_hash:0:12}… != tree ${local_hash:0:12}…)"
            ok=0
          fi
        else
          log "дым: $path — локального файла $local_file нет, сверка не выполнена"
        fi
        ;;
    esac
  done
  SMOKE_SUMMARY=$summary
  [ "$ok" -eq 1 ]
}

verify_all() {
  local rc=0
  check_manifest || rc=1
  check_modes || rc=1
  check_web || rc=1
  smoke || rc=1
  return "$rc"
}

# -------------------------------------------------------------------- службы --
restart_units() {
  local unit state status result ok
  log "службы: systemctl restart $UNITS"
  if ! sctl restart $UNITS; then
    log "службы: restart завершился ошибкой"
    return 1
  fi
  local attempt=0
  while [ "$attempt" -lt "$SERVICE_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    ok=1
    for unit in $UNITS; do
      state=$(sctl is-active "$unit" 2>/dev/null || true)
      [ "$state" = active ] || ok=0
    done
    [ "$ok" -eq 1 ] && break
    sleep 1
  done
  RESTART_LOG=""
  for unit in $UNITS; do
    state=$(sctl is-active "$unit" 2>/dev/null || true)
    status=$(sctl show -p ExecMainStatus --value "$unit" 2>/dev/null || echo "?")
    result=$(sctl show -p Result --value "$unit" 2>/dev/null || echo "?")
    log "службы: $unit is-active=$state ExecMainStatus=$status Result=$result"
    RESTART_LOG="$RESTART_LOG$unit=$state/$status/$result "
    { [ "$state" = active ] && [ "$status" = "0" ] && [ "$result" = success ]; } || return 1
  done
  return 0
}

# --------------------------------------------------------------------- откат --
restore_archive() {  # $1 = архив
  local archive=$1 parent top tmp bad
  [ -f "$archive" ] || { log "откат: архива нет: $archive"; return 1; }
  parent=$(dirname "$TARGET")
  top=$(tar tzf "$archive" 2>/dev/null | head -n 1 | cut -d/ -f1)
  [ -n "$top" ] || { log "откат: архив пуст или повреждён: $archive"; return 1; }
  tmp=$(mktemp -d "$parent/.randreport-restore.XXXXXX") || return 1
  if ! run_priv tar xzf "$archive" -C "$tmp"; then
    log "откат: распаковка не удалась"
    run_priv rm -rf "$tmp" || true
    return 1
  fi
  bad=$TARGET.failed-$(date -u +%Y%m%dT%H%M%SZ)
  if ! run_priv mv "$TARGET" "$bad"; then
    log "откат: не удалось отвести текущее дерево в $bad"
    run_priv rm -rf "$tmp" || true
    return 1
  fi
  if ! run_priv mv "$tmp/$top" "$TARGET"; then
    log "откат: не удалось вернуть дерево на место; сломанное дерево: $bad, распакованное: $tmp/$top"
    return 1
  fi
  run_priv rmdir "$tmp" 2>/dev/null || true
  log "откат: дерево восстановлено из $archive (сломанное оставлено в $bad)"
  return 0
}

latest_archive() {
  ls -1t "$BACKUP_DIR"/opt-randreport-pre-deploy-*.tgz 2>/dev/null | head -n 1 || true
}

do_rollback_inner() {  # $1 = архив
  local archive=$1
  restore_archive "$archive" || return 1
  restart_units || { log "откат: службы не поднялись после восстановления"; return 1; }
  smoke || { log "откат: дым после восстановления не прошёл ($SMOKE_SUMMARY)"; return 1; }
  return 0
}

# ----------------------------------------------------------------- состояние --
write_state() {  # $1 = статус (deployed|rolled_back)
  local status=$1 tmp
  run_priv mkdir -p "$STATE" 2>/dev/null || return 0
  cp "$MANIFEST" "$STATE/manifest.sha256" 2>/dev/null || true
  tmp=$STATE/deployed.json.tmp
  {
    printf '{\n'
    printf '  "status": "%s",\n' "$status"
    printf '  "deployed_at": "%s",\n' "$(date -Is)"
    printf '  "ref": "%s",\n' "$REF"
    printf '  "commit": "%s",\n' "$COMMIT"
    printf '  "files": %s,\n' "$FILE_COUNT"
    printf '  "manifest_ok": %s,\n' "$MANIFEST_OK"
    printf '  "source": "%s",\n' "$SOURCE"
    printf '  "target": "%s",\n' "$TARGET"
    printf '  "backup": "%s"\n' "$ARCHIVE"
    printf '}\n'
  } >"$tmp" 2>/dev/null || return 0
  run_priv chmod 0640 "$tmp" 2>/dev/null || true
  run_priv mv "$tmp" "$STATE/deployed.json" 2>/dev/null || true
  return 0
}

# -------------------------------------------------------------------- отчёты --
print_report() {  # $1 = заголовок итога
  local elapsed=$1
  emit ""
  emit "================== ОТЧЁТ ВЫКЛАДКИ randreport =================="
  emit "время:        $(date -Is)"
  emit "слот:         $LOCK (одна выкладка за раз; вторая ждёт)"
  emit "источник:     $SOURCE  ref=$REF  commit=$COMMIT"
  emit "приёмник:     $TARGET  ($OWNER, каталоги 0750, файлы 0640, исполняемые 0750, .venv исключён)"
  emit "резерв:       ${ARCHIVE:-нет}  (откат — распаковка этого архива)"
  emit "манифест:     $MANIFEST_OK/$FILE_COUNT OK  (sha256sum -c из git-списка)"
  [ -n "$ACTUALITY" ] && emit "актуальность: $ACTUALITY"
  emit "службы:       $RESTART_LOG"
  emit "web check:    страниц $PAGES, проблем $PROBLEMS"
  emit "дым:          $SMOKE_SUMMARY  ($SMOKE_BASE)"
  emit ""
  emit "$elapsed"
  emit "==============================================================="
}

usage() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------- команды ----
do_deploy() {
  acquire_slot x
  trap cleanup EXIT
  preflight
  wait_for_idle_units
  prepare_stage
  backup_target || die 2 "не удалось снять резерв в $BACKUP_DIR"
  if ! copy_tree; then
    fail_after_copy "копирование в $TARGET не удалось"
  fi
  if ! restart_units; then
    fail_after_copy "службы не поднялись после restart"
  fi
  if ! verify_all; then
    fail_after_copy "проверки после выкладки не прошли"
  fi
  write_state deployed
  print_report "ИТОГ: ВЫКЛАДКА УСПЕШНА — commit $COMMIT, файлов $FILE_COUNT, манифест $MANIFEST_OK/$FILE_COUNT, web check $PAGES/$PROBLEMS, дым $SMOKE_SUMMARY"
  return 0
}

fail_after_copy() {
  local reason=$1
  emit ""
  log "выкладка: ПРОВАЛ — $reason; откатываемся к ${ARCHIVE:-<архива нет>}"
  if do_rollback_inner "$ARCHIVE"; then
    write_state rolled_back
    print_report "ИТОГ: ВЫКЛАДКА ПРОВАЛЕНА ($reason) — сайт ОТКАЧЕН из $ARCHIVE, дым после отката: $SMOKE_SUMMARY"
    exit 4
  fi
  write_state failed
  print_report "ИТОГ: ВЫКЛАДКА ПРОВАЛЕНА ($reason) И ОТКАТ НЕ УДАЛСЯ — сайт может быть нездоров, нужен человек (архив: ${ARCHIVE:-нет})"
  exit 5
}

do_verify() {
  acquire_slot s
  trap cleanup EXIT
  preflight
  # verify проверяет ВЫЛОЖЕННОЕ дерево: коммит берётся из состояния последней
  # выкладки, а расхождение с текущим main сообщается отдельной строкой (main мог
  # уйти вперёд, и это не дефект выкладки, а факт для карточки).
  local deployed="" tip short_tip short_deployed
  tip=$(git -C "$SOURCE" rev-parse "$REF^{commit}")
  short_tip=$(git -C "$SOURCE" rev-parse --short "$tip")
  if [ -s "$STATE/deployed.json" ]; then
    deployed=$(sed -n 's/.*"commit": *"\([0-9a-f]\{7,\}\)".*/\1/p' "$STATE/deployed.json" | head -n 1)
  fi
  if [ -n "$deployed" ] && git -C "$SOURCE" cat-file -e "$deployed^{commit}" 2>/dev/null; then
    COMMIT=$deployed
    short_deployed=$(git -C "$SOURCE" rev-parse --short "$COMMIT")
    if [ "$COMMIT" != "$tip" ]; then
      ACTUALITY="main ушёл вперёд: выложен $short_deployed, $REF сейчас $short_tip"
      log "verify: выложен $short_deployed, $REF сейчас $short_tip — проверяется выложенное дерево"
    else
      ACTUALITY="дерево соответствует $REF ($short_tip)"
    fi
  else
    COMMIT=$tip
    ACTUALITY="состояния выкладки нет ($STATE) — проверяется текущий $REF ($short_tip)"
  fi
  FILE_COUNT=$(list_paths "$COMMIT" | wc -l | tr -d ' ')
  EXEC_COUNT=$(list_exec_paths "$COMMIT" | wc -l | tr -d ' ')
  DIR_COUNT=$(list_dirs "$COMMIT" | wc -l | tr -d ' ')
  if [ -s "$STATE/manifest.sha256" ]; then
    MANIFEST=$STATE/manifest.sha256
  else
    prepare_stage
  fi
  local rc=0
  verify_all || rc=1
  if [ "$rc" -eq 0 ]; then
    print_report "ИТОГ: ПРОВЕРКИ ПРОЙДЕНЫ — манифест $MANIFEST_OK/$FILE_COUNT, web check $PAGES/$PROBLEMS, дым $SMOKE_SUMMARY"
  else
    print_report "ИТОГ: ПРОВЕРКИ НЕ ПРОЙДЕНЫ — см. строки выше (манифест $MANIFEST_OK/$FILE_COUNT, web check $PAGES/$PROBLEMS, дым $SMOKE_SUMMARY)"
  fi
  return "$rc"
}

do_rollback() {
  local archive=${1:-}
  acquire_slot x
  trap cleanup EXIT
  if [ -z "$archive" ]; then
    archive=$(latest_archive)
    [ -n "$archive" ] || die 2 "в $BACKUP_DIR нет архивов выкладки opt-randreport-pre-deploy-*.tgz"
  fi
  log "ручной откат: $archive -> $TARGET"
  if ! do_rollback_inner "$archive"; then
    print_report "ИТОГ: ОТКАТ НЕ УДАЛСЯ ($archive) — нужен человек"
    return 5
  fi
  ARCHIVE=$archive
  print_report "ИТОГ: ОТКАТ ВЫПОЛНЕН из $archive, дым $SMOKE_SUMMARY"
  return 0
}

do_slot() {  # выполнить произвольную команду под слотом выкладки
  [ "$#" -gt 0 ] || die 2 "slot: нужна команда"
  acquire_slot x
  trap cleanup EXIT
  "$@"
}

case "${1:-deploy}" in
  deploy)   shift || true; do_deploy "$@" ;;
  verify)   shift || true; do_verify "$@" ;;
  rollback) shift || true; do_rollback "$@" ;;
  slot)     shift; do_slot "$@" ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
