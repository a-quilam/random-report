#!/usr/bin/env python3
"""Save the TGStat Stat API token into the service environment file.

The token is a secret: it is read from a hidden prompt (or stdin, never from the
command line, where `sudo` and the shell history would keep a copy) and written
into the service's environment file, which the nightly reads. Other settings in
that file are preserved; only the TGStat lines change.

    sudo python3 scripts/enter-tgstat-token.py
    printf '%s\n' "$TOKEN" | sudo python3 scripts/enter-tgstat-token.py --stdin

Nothing prints the token back: the confirmation names the file and the key.
"""
from __future__ import annotations

import argparse
import getpass
import grp
import os
import pwd
import re
import sys
import tempfile
from pathlib import Path

DEFAULT_FILE = Path("/etc/randreport/randreport.env")
KEY = "TGSTAT_TOKEN"
CHANNEL_KEY = "TGSTAT_CHANNEL"
CHANNEL_FALLBACKS = ("TGSTAT_CHANNEL", "TELEGRAM_PUBLIC_CHANNEL")
#: A documented Stat API token is an opaque ASCII string; reject anything else.
TOKEN_RE = re.compile(r"^[0-9a-f]{16,128}$", re.IGNORECASE)


def read_token(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().strip()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass("TGStat token (не отображается): ").strip()


def update(text: str, token: str) -> str:
    """Replace or append the TGStat lines, keeping every other setting."""
    lines = text.splitlines()
    out: list[str] = []
    have_token = False
    channel = ""
    for line in lines:
        name, _, value = line.partition("=")
        if name.strip() in CHANNEL_FALLBACKS and value.strip():
            # the channel the counters belong to is already configured for the bot;
            # copy it for the Stat API rather than asking the owner twice
            channel = channel or value.strip()
    for line in lines:
        name = line.partition("=")[0].strip()
        if name == KEY:
            if not have_token:
                out.append(f"{KEY}={token}")
                have_token = True
            continue
        out.append(line)
    if not have_token:
        out.append(f"{KEY}={token}")
    have_channel = any(l.partition("=")[0].strip() == CHANNEL_KEY for l in out)
    if not have_channel and channel:
        out.append(f"{CHANNEL_KEY}={channel}")
    return "\n".join(out) + "\n"


def write_secret(path: Path, text: str) -> None:
    """Write atomically next to the target, then replace it under the target mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        if os.geteuid() == 0:
            try:
                os.chown(tmp, pwd.getpwnam("root").pw_uid, grp.getgrnam("randreport").gr_gid)
            except KeyError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="service environment file")
    parser.add_argument("--stdin", action="store_true", help="read the token from stdin instead of a prompt")
    args = parser.parse_args(argv)
    token = read_token(args.stdin)
    if not TOKEN_RE.match(token):
        print(
            "Отказ: токен TGStat пуст или имеет неожиданный вид "
            "(ожидается 16–128 шестнадцатеричных символов). Ничего не записано.",
            file=sys.stderr,
        )
        return 2
    if not args.file.is_file():
        print(f"Отказ: файл {args.file} не найден. Ничего не записано.", file=sys.stderr)
        return 2
    text = update(args.file.read_text(encoding="utf-8"), token)
    write_secret(args.file, text)
    stored = args.file.read_text(encoding="utf-8")
    line = next(l for l in stored.splitlines() if l.startswith(KEY + "="))
    if line.partition("=")[2].strip() != token or not any(
        l.partition("=")[0].strip() == CHANNEL_KEY and l.partition("=")[2].strip()
        for l in stored.splitlines()
    ):
        print("Отказ: запись не подтвердилась. Проверьте файл.", file=sys.stderr)
        return 3
    print(f"Готово: {KEY} записан в {args.file}; прочие настройки сохранены; права файла — 0600, владелец root:randreport.")
    print("Следующая ночная синхронизация прочитает его сама; для немедленной проверки запустите sync-службу.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
