#!/bin/sh
# Ежедневный зашифрованный бэкап с проверкой восстановления и retention.
#
# Запускается из deploy/randreport-backup.timer. Пароль шифрования берётся из
# окружения (EnvironmentFile=/etc/randreport/backup.env, права 0600) и нигде не
# печатается: он попадает только в среду дочернего openssl.
#
# Инварианты:
#   * копия всегда шифруется (*.enc); незашифрованная копия приватной БД на диск
#     не пишется;
#   * восстановление проверяется в приватном временном файле сразу после бэкапа,
#     и временный файл удаляется — «наличие бэкапа не доказывает восстановление»;
#   * retention никогда не удаляет единственную копию (prune-backups это
#     гарантирует и тестируется).
#
# Ничего, кроме вывода manifest/verify (без секретов), в лог не попадает.

set -eu

PYTHON="${RANREPORT_PYTHON:-/opt/randreport/.venv/bin/python}"
DB="${RANREPORT_DB:-/var/lib/randreport/randreport.sqlite}"
DIR="${RANREPORT_BACKUP_DIR:-/var/backups/randreport}"
KEEP_DAILY="${RANREPORT_BACKUP_KEEP_DAILY:-14}"
KEEP_MONTHLY="${RANREPORT_BACKUP_KEEP_MONTHLY:-1}"

if [ -z "${RANREPORT_BACKUP_PASSPHRASE:-}" ]; then
    echo "RANREPORT_BACKUP_PASSPHRASE is not set; refusing to write a plaintext backup" >&2
    exit 2
fi

mkdir -p "$DIR"
STAMP="$(date -u +%Y-%m-%d)"
OUT="$DIR/randreport-$STAMP.sqlite.enc"

"$PYTHON" -m randreport.ops.cli backup --db "$DB" --out "$OUT" --encrypt

# Проверка восстановления на чистой копии (temp удаляется сразу).
RESTORE_CHECK="$(mktemp "${TMPDIR:-/tmp}/randreport-restore-check.XXXXXX")"
trap 'rm -f "$RESTORE_CHECK"' EXIT
rm -f "$RESTORE_CHECK"
"$PYTHON" -m randreport.ops.cli restore --backup "$OUT" --db "$RESTORE_CHECK" --force
rm -f "$RESTORE_CHECK"

"$PYTHON" -m randreport.ops.cli prune-backups --out "$DIR" \
    --keep-daily "$KEEP_DAILY" --keep-monthly "$KEEP_MONTHLY"
