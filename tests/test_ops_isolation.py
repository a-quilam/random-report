"""Installation kit and public-process isolation (card t_14543ca6).

Everything asserted here is exercised for real: the unit files are parsed as the
deployment ships them, the redirect and HTTP-error paths go through the real
``UrllibTransport`` against a synthetic loopback HTTP server, the public site is
run both in-process and as a real subprocess with the private database present but
unreachable, and the encrypted backup is written and restored with the system
``openssl``. Only synthetic data is used; no external service, no token and no
private DB content is read.
"""

from __future__ import annotations

import gc
import http.client
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from randreport import bindguard, leakguard, redaction
from randreport.connectors.transport import (
    HttpStatusError,
    Request,
    TransportError,
    UrllibTransport,
)
from randreport.ops import backup as backup_module
from randreport.ops.backup import (
    BackupError,
    backup_database,
    prune_backups,
    restore_database,
)
from randreport.ops.publish import PublishError, rollback_release
from randreport.store import apply_migrations, connect
from randreport.store import repository as repo
from randreport.web import reports as reports_module
from randreport.web.app import PublicSite
from randreport.web.release_index import ReleaseIndex
from tests import dev_release

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"

#: Credentials that must never appear in the public unit.
PUBLIC_FORBIDDEN_ENV = (
    "ZENMONEY_TOKEN",
    "TEAMUP_TOKEN",
    "TEAMUP_CALENDAR_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_PUBLIC_CHANNEL",
    "RANREPORT_ADMIN_SECRET",
    "RANREPORT_BACKUP_PASSPHRASE",
)


def _read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def _directives(name: str) -> str:
    """Only the active (non-comment, non-blank) lines of a unit file.

    Comments deliberately *document* the isolation invariant ("no EnvironmentFile
    here"), so they are not what the check must look at -- the directives are.
    """

    lines = [
        line.rstrip()
        for line in _read(name).splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]
    return "\n".join(lines)


def _exec_start(name: str) -> str:
    match = re.search(r"^ExecStart=(.+)$", _read(name), re.MULTILINE)
    assert match is not None, f"{name} has no ExecStart"
    return match.group(1).strip()


def _unit_reference(name: str) -> str | None:
    match = re.search(r"^Unit=(.+)$", _read(name), re.MULTILINE)
    return match.group(1).strip() if match else None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_get(port: int, path: str, *, timeout: float = 5.0) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        return response.status, body
    finally:
        conn.close()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """A synthetic origin that records the headers it receives."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # pragma: no cover - keep the test output clean
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.seen.append(dict(self.headers))  # type: ignore[attr-defined]
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.target)  # type: ignore[attr-defined]
            self.end_headers()
            return
        if self.path == "/same-redirect":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        if self.path == "/error":
            body = b"upstream failure: token=SECRETLEAKVALUE"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    """A loopback HTTP server on its own thread, always shut down again."""

    def __init__(self, target: str | None = None) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.httpd.daemon_threads = True
        self.httpd.seen = []  # type: ignore[attr-defined]
        self.httpd.target = target or ""  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def seen(self) -> list[dict[str, str]]:
        return self.httpd.seen  # type: ignore[attr-defined]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# 1. The installation kit (deploy/)
# --------------------------------------------------------------------------- #

class DeploymentKitTests(unittest.TestCase):
    def test_all_units_for_the_three_roles_exist(self) -> None:
        for name in (
            "randreport-public.service",
            "randreport-admin.service",
            "randreport-sync.service",
            "randreport-sync.timer",
            "randreport-backup.service",
            "randreport-backup.timer",
            "randreport-backup.sh",
            "randreport-deploy.sh",
        ):
            self.assertTrue((DEPLOY / name).is_file(), f"missing deploy/{name}")

    def test_public_unit_has_no_secrets_and_no_private_db(self) -> None:
        text = _directives("randreport-public.service")
        self.assertNotIn("EnvironmentFile", text, "the public process must not load a secret file")
        for name in PUBLIC_FORBIDDEN_ENV:
            self.assertNotIn(name, text, f"{name} must never reach the public process")
        self.assertNotIn("RANREPORT_DB", text, "the public process must not point at the private DB")
        self.assertIn("RANREPORT_RELEASE_DIR=/var/www/randreport", text)
        self.assertIn("ReadOnlyPaths=/var/www/randreport", text)
        self.assertNotIn("ReadWritePaths", text, "the public process writes nothing")

    def test_public_unit_hardens_and_binds_loopback(self) -> None:
        text = _read("randreport-public.service")
        self.assertIn("User=randreport", text)
        self.assertIn("NoNewPrivileges=true", text)
        self.assertIn("ProtectSystem=strict", text)
        self.assertIn("ProtectHome=true", text)
        self.assertIn("PrivateTmp=true", text)
        self.assertIn("--host 127.0.0.1", _exec_start("randreport-public.service"))
        self.assertNotIn("RANREPORT_ALLOW_EXTERNAL_BIND", text)

    def test_admin_unit_uses_a_secret_file_and_loopback(self) -> None:
        text = _read("randreport-admin.service")
        self.assertIn("EnvironmentFile=/etc/randreport/admin.env", text)
        self.assertIn("ReadWritePaths=/var/lib/randreport", text)
        self.assertIn("--host 127.0.0.1", _exec_start("randreport-admin.service"))
        self.assertNotIn("RANREPORT_ALLOW_EXTERNAL_BIND", text)

    def test_sync_unit_runs_the_nightly_cli_from_a_secret_file(self) -> None:
        text = _read("randreport-sync.service")
        self.assertIn("Type=oneshot", text)
        self.assertIn("EnvironmentFile=/etc/randreport/randreport.env", text)
        self.assertIn("randreport.ops.cli run", _exec_start("randreport-sync.service"))
        self.assertIn("ReadWritePaths=/var/lib/randreport /var/www/randreport", text)

    def test_sync_timer_is_0300_moscow_and_points_at_the_sync_unit(self) -> None:
        text = _read("randreport-sync.timer")
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow", text)
        self.assertIn("Persistent=true", text)
        self.assertNotIn("America/", text)
        self.assertEqual(_unit_reference("randreport-sync.timer"), "randreport-sync.service")

    def test_backup_unit_and_timer_are_wired(self) -> None:
        self.assertEqual(_unit_reference("randreport-backup.timer"), "randreport-backup.service")
        self.assertIn("OnCalendar=", _read("randreport-backup.timer"))
        self.assertIn("Europe/Moscow", _read("randreport-backup.timer"))
        self.assertIn("EnvironmentFile=/etc/randreport/backup.env", _read("randreport-backup.service"))

    def test_backup_script_encrypts_and_verifies_the_restore(self) -> None:
        text = _read("randreport-backup.sh")
        self.assertIn("--encrypt", text)
        self.assertIn("backup --db", text)
        self.assertIn("restore --backup", text)
        self.assertIn("prune-backups", text)
        self.assertIn("RANREPORT_BACKUP_PASSPHRASE is not set", text)
        self.assertIn("rm -f", text)
        # the passphrase is never echoed on the command line
        self.assertNotIn("--pass", text)
        self.assertNotIn("--password", text)

    def test_every_unit_exec_start_target_exists(self) -> None:
        for name in ("randreport-public.service", "randreport-admin.service", "randreport-sync.service"):
            exec_start = _exec_start(name)
            module = re.search(r"-m (\S+)", exec_start)
            assert module is not None, exec_start
            rel = Path(module.group(1).replace(".", "/"))
            found = (ROOT / rel).with_suffix(".py") if (ROOT / rel).with_suffix(".py").exists() else (ROOT / rel / "__main__.py")
            self.assertTrue(found.exists(), f"{name}: {module.group(1)} is not importable ({found})")

    def test_no_unit_file_carries_a_token_shaped_secret(self) -> None:
        for path in DEPLOY.glob("randreport-*.serv*"):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("#") or "=" not in line:
                    continue
                value = line.split("=", 1)[1].strip()
                self.assertLess(len(value), 200, f"{path.name}: suspicious long value")
                self.assertFalse(
                    re.search(r"\b[A-Za-z0-9_\-]{24,}\b", value) and redaction._looks_like_secret(value),
                    f"{path.name}: looks like an embedded secret",
                )

    def test_schedule_module_matches_the_shipped_timer(self) -> None:
        from randreport.ops.schedule import systemd_timer_text

        text = systemd_timer_text()
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow", text)
        self.assertIn("Persistent=true", text)
        self.assertIn("Unit=randreport-sync.service", text)


# --------------------------------------------------------------------------- #
# 2. Loopback by default, external publication refused
# --------------------------------------------------------------------------- #

class BindGuardTests(unittest.TestCase):
    def test_loopback_literals_and_defaults(self) -> None:
        for host in (None, "", "127.0.0.1", "127.0.0.5", "::1", "localhost", "[::1]"):
            self.assertTrue(bindguard.is_loopback(host), host)

    def test_routable_and_wildcard_hosts_are_not_loopback(self) -> None:
        for host in ("0.0.0.0", "::", "10.0.0.5", "8.8.8.8", "example.com", "randreport.example"):
            self.assertFalse(bindguard.is_loopback(host), host)

    def test_external_bind_needs_the_flag_and_the_env_opt_in(self) -> None:
        # neither
        with self.assertRaises(bindguard.BindRefused):
            bindguard.assert_bind_allowed("0.0.0.0", environ={})
        # only the env opt-in
        with self.assertRaises(bindguard.BindRefused):
            bindguard.assert_bind_allowed("0.0.0.0", environ={bindguard.ALLOW_ENV: "1"})
        # only the flag
        with self.assertRaises(bindguard.BindRefused):
            bindguard.assert_bind_allowed("0.0.0.0", allow_external=True, environ={})
        # both: allowed
        bindguard.assert_bind_allowed("0.0.0.0", allow_external=True, environ={bindguard.ALLOW_ENV: "1"})

    def test_loopback_is_allowed_without_any_opt_in(self) -> None:
        bindguard.assert_bind_allowed("127.0.0.1", environ={})
        bindguard.assert_bind_allowed(None, environ={})

    def test_web_serve_refuses_a_public_bind_and_never_opens_a_socket(self) -> None:
        from randreport.web import __main__ as web_main

        code = web_main.main(["serve", "--host", "0.0.0.0", "--port", str(_free_port())])
        self.assertEqual(code, 2)

    def test_admin_serve_refuses_a_public_bind(self) -> None:
        from randreport.admin import __main__ as admin_main

        saved = os.environ.get("RANREPORT_ADMIN_SECRET")
        os.environ["RANREPORT_ADMIN_SECRET"] = "test-secret-value-not-a-token"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = str(Path(tmp) / "private.sqlite")
                code = admin_main.main(
                    ["serve", "--db", db, "--host", "0.0.0.0", "--port", str(_free_port())]
                )
        finally:
            if saved is None:
                os.environ.pop("RANREPORT_ADMIN_SECRET", None)
            else:
                os.environ["RANREPORT_ADMIN_SECRET"] = saved
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------- #
# 3. Redirect credential ban and HTTPError close
# --------------------------------------------------------------------------- #

class TransportRedirectTests(unittest.TestCase):
    def test_credentials_are_not_forwarded_to_another_origin(self) -> None:
        other = _Server()
        origin = _Server(target=f"http://127.0.0.1:{other.port}/steal")
        try:
            transport = UrllibTransport(timeout_s=5)
            request = Request(
                "GET",
                f"http://127.0.0.1:{origin.port}/redirect",
                headers={"Authorization": "Bearer SECRETTOKENVALUE"},
            )
            with self.assertRaises(TransportError):
                transport.send(request)
            # the credential never reached the other origin: it was never called
            self.assertEqual(other.seen, [])
        finally:
            origin.close()
            other.close()

    def test_a_same_origin_redirect_drops_the_credential_header(self) -> None:
        origin = _Server()
        try:
            transport = UrllibTransport(timeout_s=5)
            request = Request(
                "GET",
                f"http://127.0.0.1:{origin.port}/same-redirect",
                headers={"Authorization": "Bearer SECRETTOKENVALUE"},
            )
            response = transport.send(request)
            self.assertEqual(response.status, 200)
            self.assertEqual(len(origin.seen), 2)
            # first hop carried it; the redirected hop must not replay it
            self.assertIn("Authorization", origin.seen[0])
            self.assertNotIn("Authorization", origin.seen[1])
        finally:
            origin.close()

    def test_http_error_body_is_redacted_and_the_response_is_closed(self) -> None:
        origin = _Server()
        try:
            transport = UrllibTransport(timeout_s=5)
            with self.assertRaises(HttpStatusError) as raised:
                transport.send(Request("GET", f"http://127.0.0.1:{origin.port}/error"))
            self.assertEqual(raised.exception.status, 500)
            self.assertNotIn("SECRETLEAKVALUE", str(raised.exception))
            self.assertIn("token=***", raised.exception.body)
        finally:
            origin.close()
        gc.collect()
        open_responses = [item for item in leakguard.open_resources() if item.kind == "http response"]
        self.assertEqual(open_responses, [], "an HTTPError response was left open")


class RedactionTests(unittest.TestCase):
    def test_query_parameters_and_headers_are_masked(self) -> None:
        self.assertIn("token=***", redaction.redact("https://api/?token=AbCd1234EfGr&x=1"))
        self.assertNotIn("AbCd1234EfGr", redaction.redact("token=AbCd1234EfGr"))
        self.assertEqual(redaction.redact_url("https://h/p?token=SEKRET&q=1"), "https://h/p?***")
        self.assertNotIn("SECRET", redaction.redact("Authorization: Bearer SECRETvalue123"))
        self.assertNotIn("hjkl", redaction.redact("key=hjkl1234zxcv"))

    def test_telegram_bot_path_is_masked(self) -> None:
        text = redaction.redact("GET https://api.telegram.org/bot8123456:AAFsecretsecretsecret/getMe")
        self.assertNotIn("AAFsecretsecretsecret", text)
        self.assertIn("/bot***", text)

    def test_headers_and_mappings_are_masked(self) -> None:
        headers = redaction.redact_headers({"Authorization": "Bearer x", "Accept": "application/json"})
        self.assertEqual(headers["Authorization"], redaction.MASK)
        self.assertEqual(headers["Accept"], "application/json")
        record = redaction.redact_mapping({"url": "https://h/p", "token": "SEKRET", "params": {"key": "abc123XYZ"}})
        self.assertEqual(record["token"], redaction.MASK)
        self.assertEqual(record["params"]["key"], redaction.MASK)

    def test_redaction_never_raises_on_arbitrary_values(self) -> None:
        for value in (None, 42, b"bytes", {}, "no secrets here"):
            redaction.redact(value)  # type: ignore[arg-type]

    def test_a_request_record_for_logs_carries_no_credential(self) -> None:
        request = Request(
            "GET",
            "https://api.zenmoney.ru/v8/diff/",
            params={"token": "AbCd1234EfGh5678", "key": "ZzYyXxWw9988"},
            headers={"Authorization": "Bearer SECRET"},
        )
        record = request.as_record()
        dumped = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("AbCd1234EfGh5678", dumped)
        self.assertNotIn("ZzYyXxWw9988", dumped)
        self.assertEqual(record["params"]["token"], redaction.MASK)


# --------------------------------------------------------------------------- #
# 4. Encrypted backup, restore test and retention
# --------------------------------------------------------------------------- #

def _synthetic_db(path: Path) -> None:
    conn = connect(path)
    try:
        apply_migrations(conn)
        repo.audit(conn, action="synthetic", entity_type="test", entity_key="k", detail={"n": 1}, now="2025-07-01T00:00:00+00:00")
    finally:
        conn.close()


class EncryptedBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "private.sqlite"
        _synthetic_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @unittest.skipUnless(backup_module.openssl_available(), "openssl is required")
    def test_encrypted_backup_restores_on_a_clean_instance(self) -> None:
        target = self.dir / "randreport-2025-07-01.sqlite.enc"
        manifest = backup_database(self.db, target, passphrase="correct horse battery")
        self.assertTrue(manifest["encrypted"])
        self.assertEqual(manifest["cipher"], backup_module.CIPHER)
        self.assertEqual(manifest["integrity"], "ok")
        # the file on disk is ciphertext, not a readable SQLite database
        self.assertEqual(target.read_bytes()[:8], b"Salted__")
        self.assertNotIn(b"SQLite format 3", target.read_bytes()[:64])

        restored = self.dir / "restored.sqlite"
        report = restore_database(target, restored, passphrase="correct horse battery")
        self.assertTrue(report["usable"])
        self.assertEqual(report["row_counts"]["audit_log"], 1)

    @unittest.skipUnless(backup_module.openssl_available(), "openssl is required")
    def test_wrong_or_missing_passphrase_is_refused(self) -> None:
        target = self.dir / "randreport-2025-07-01.sqlite.enc"
        backup_database(self.db, target, passphrase="right-passphrase")
        with self.assertRaises(BackupError):
            restore_database(target, self.dir / "a.sqlite")
        with self.assertRaises(BackupError):
            restore_database(target, self.dir / "b.sqlite", passphrase="wrong-passphrase")
        self.assertFalse((self.dir / "a.sqlite").exists())
        self.assertFalse((self.dir / "b.sqlite").exists())

    def test_encrypted_backup_needs_the_enc_suffix(self) -> None:
        with self.assertRaises(BackupError):
            backup_database(self.db, self.dir / "plain.sqlite", passphrase="x" * 12)

    def test_plaintext_backup_still_works(self) -> None:
        target = self.dir / "plain.sqlite"
        manifest = backup_database(self.db, target)
        self.assertFalse(manifest["encrypted"])
        report = restore_database(target, self.dir / "restored-plain.sqlite")
        self.assertTrue(report["usable"])

    def test_prune_never_deletes_the_only_copy(self) -> None:
        backups = self.dir / "only"
        backups.mkdir()
        only = backups / "randreport-2020-01-01.sqlite"
        only.write_bytes(b"x")
        report = prune_backups(backups, keep_daily=1, keep_monthly=0)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["reason"], "single_copy")
        self.assertTrue(only.exists())

    def test_prune_keeps_the_newest_and_a_monthly_copy(self) -> None:
        backups = self.dir / "many"
        backups.mkdir()
        made: list[Path] = []
        # 20 daily copies spanning three months, oldest first
        for index in range(20):
            path = backups / f"randreport-2020-{index:02d}-01.sqlite"
            path.write_bytes(b"x")
            stamp = time.time() - (20 - index) * 86400
            os.utime(path, (stamp, stamp))
            made.append(path)
        report = prune_backups(backups, keep_daily=5, keep_monthly=1)
        survivors = [p for p in made if p.exists()]
        self.assertGreaterEqual(len(survivors), 1, "prune must never empty the directory")
        self.assertIn(made[-1], survivors, "the newest copy is always kept")
        self.assertGreaterEqual(len(survivors), 5)
        self.assertEqual(len(report["deleted"]), len(made) - len(survivors))

    def test_cli_encrypt_requires_the_passphrase_env(self) -> None:
        from randreport.ops import cli

        target = self.dir / "cli.sqlite.enc"
        env = {k: v for k, v in os.environ.items() if k != backup_module.PASSPHRASE_ENV}
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            code = cli.main(["backup", "--db", str(self.db), "--out", str(target), "--encrypt"])
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(code, 2)
        self.assertFalse(target.exists())

    @unittest.skipUnless(backup_module.openssl_available(), "openssl is required")
    def test_cli_backup_and_restore_round_trip(self) -> None:
        from randreport.ops import cli

        target = self.dir / "cli.sqlite.enc"
        saved = os.environ.get(backup_module.PASSPHRASE_ENV)
        os.environ[backup_module.PASSPHRASE_ENV] = "cli-passphrase"
        try:
            self.assertEqual(cli.main(["backup", "--db", str(self.db), "--out", str(target), "--encrypt"]), 0)
            self.assertEqual(
                cli.main(["restore", "--backup", str(target), "--db", str(self.dir / "cli-restored.sqlite")]),
                0,
            )
        finally:
            if saved is None:
                os.environ.pop(backup_module.PASSPHRASE_ENV, None)
            else:
                os.environ[backup_module.PASSPHRASE_ENV] = saved


# --------------------------------------------------------------------------- #
# 5. The public process serves from the allowlist store, not the private DB
# --------------------------------------------------------------------------- #

class PublicProcessIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "private.sqlite"
        self.release_dir = self.root / "releases"
        self.release_dir.mkdir(parents=True, exist_ok=True)
        # the first release is built before the archive exists: empty reports.json
        self.old_release = dev_release.build(
            str(self.release_dir), period="2025-06", db_path=str(self.db)
        )["release_id"]
        # seed the report archive, then publish a release that carries it
        conn = connect(self.db)
        try:
            apply_migrations(conn)
            reports_module.seed_published_snapshots(conn, published_at="2026-01-10T00:00:00+00:00")
        finally:
            conn.close()
        dev_release.build(str(self.release_dir), period="2025-07", db_path=str(self.db))
        # pin the publication order so the test does not depend on the clock
        os.utime(self.release_dir / self.old_release, (1000, 1000))
        for entry in self.release_dir.iterdir():
            if entry.name.startswith("rel_") and entry.name != self.old_release:
                os.utime(entry, (2000, 2000))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_archive_is_published_into_the_release(self) -> None:
        index = ReleaseIndex(self.release_dir)
        archive = index.reports()
        self.assertIn("2025-01", archive, "the seeded report archive must be part of the release")
        entry = index.report_entry("2025-01")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertTrue(entry["versions"])

    def test_report_page_renders_without_any_private_db(self) -> None:
        site = PublicSite(release_dir=self.release_dir, db_path=None)
        try:
            self.assertIsNone(site.connection(), "with no db path the site must not hold a connection")
            status, body = self._dispatch(site, "/reports/2025-01")
            text = body.decode("utf-8")
            self.assertEqual(status, 200)
            self.assertIn("Версии", text)
            self.assertIn("v1", text)
            # every page still renders with no database at all
            for route in ("/", "/money", "/events", "/reports", "/chronicle"):
                self.assertEqual(self._dispatch(site, route)[0], 200, route)
        finally:
            site.close()

    def test_nonexistent_report_db_path_does_not_break_the_site(self) -> None:
        site = PublicSite(release_dir=self.release_dir, db_path=self.root / "missing.sqlite")
        try:
            self.assertIsNone(site.connection())
            self.assertEqual(self._dispatch(site, "/reports/2025-01")[0], 200)
        finally:
            site.close()

    def test_rollback_to_a_release_without_the_archive_keeps_reports_alive(self) -> None:
        """A rollback to an older release must not take ``/reports/*`` down."""

        from randreport.ops.publish import rollback_release

        rollback_release(self.release_dir, self.old_release)
        index = ReleaseIndex(self.release_dir)
        self.assertEqual(index.current_id(), self.old_release)
        # the current release carries an empty archive, so the fallback finds one
        archive = index.reports()
        self.assertIn("2025-01", archive, "the archive must survive a rollback")
        site = PublicSite(release_dir=self.release_dir, db_path=None)
        try:
            status, body = self._dispatch(site, "/reports/2025-01")
            self.assertEqual(status, 200)
            self.assertIn("Версии", body.decode("utf-8"))
        finally:
            site.close()

    def _dispatch(self, site: PublicSite, route: str) -> tuple[int, bytes]:
        response = site._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": ""})
        return response.status, response.body

    def test_serve_subprocess_starts_restarts_and_ignores_the_private_db(self) -> None:
        """A real start / restart with RANREPORT_DB present but never opened."""

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["RANREPORT_RELEASE_DIR"] = str(self.release_dir)
        env["RANREPORT_DB"] = str(self.db)  # present on purpose: serve must ignore it
        for attempt in range(2):
            port = _free_port()
            env["RANREPORT_PORT"] = str(port)
            proc = subprocess.Popen(
                [sys.executable, "-m", "randreport.web", "serve", "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                status, body = None, b""
                for _ in range(100):
                    try:
                        status, body = _http_get(port, "/healthz")
                        break
                    except OSError:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                self.assertEqual(status, 200, f"run {attempt}: server did not come up")
                self.assertIn(b'"ok"', body)
                # the report archive is served, so the private DB is not needed
                report_status, report_body = _http_get(port, "/reports/2025-01")
                self.assertEqual(report_status, 200)
                self.assertIn("Версии".encode("utf-8"), report_body)
            finally:
                proc.terminate()
                try:
                    out = proc.communicate(timeout=10)[0]
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
                    out = proc.communicate()[0]
            self.assertIn("private db: not opened", out, "serve attached the private database")
            self.assertNotIn("Traceback", out)


# --------------------------------------------------------------------------- #
# 6. Verifiable rollback
# --------------------------------------------------------------------------- #

class RollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release_dir = self.root / "releases"
        self.release_dir.mkdir(parents=True, exist_ok=True)
        db = self.root / "private.sqlite"
        self.older = dev_release.build(str(self.release_dir), period="2025-06", db_path=str(db))["release_id"]
        self.newer = dev_release.build(str(self.release_dir), period="2025-07", db_path=str(db))["release_id"]
        self.assertNotEqual(self.older, self.newer)
        # publication order is not derivable from the content-addressed ids; pin it
        os.utime(self.release_dir / self.older, (1000, 1000))
        os.utime(self.release_dir / self.newer, (2000, 2000))
        self.ids = sorted([self.older, self.newer])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _current(self) -> str | None:
        return os.readlink(self.release_dir / "current")

    def test_rollback_moves_the_pointer_and_verifies(self) -> None:
        self.assertEqual(self._current(), self.newer, "the last build is the published one")
        report = rollback_release(self.release_dir, self.older)
        self.assertEqual(report["release_id"], self.older)
        self.assertTrue(report["verified"])
        self.assertEqual(self._current(), self.older)
        # the release still loads through the allowlist after the pointer moved
        self.assertIsNotNone(ReleaseIndex(self.release_dir).current())

    def test_rollback_refuses_an_unknown_or_invalid_release(self) -> None:
        with self.assertRaises(PublishError):
            rollback_release(self.release_dir, "rel_does_not_exist")
        (self.release_dir / "rel_bad").mkdir()
        (self.release_dir / "rel_bad" / "index.json").write_text(
            json.dumps({"release_id": "rel_bad", "as_of": "2025-01-01", "sections": ["money"]}), encoding="utf-8"
        )
        (self.release_dir / "rel_bad" / "money.json").write_text(
            json.dumps({"account_ids": ["acc_secret"]}), encoding="utf-8"
        )
        with self.assertRaises(PublishError):
            rollback_release(self.release_dir, "rel_bad")
        self.assertNotEqual(self._current(), "rel_bad")

    def test_cli_rollback_dry_run_to_and_previous(self) -> None:
        from randreport.ops import cli

        code = cli.main(["rollback", "--publish-dir", str(self.release_dir), "--to", self.older, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self._current(), self.newer, "dry-run must not move the pointer")
        code = cli.main(["rollback", "--publish-dir", str(self.release_dir), "--to", self.older])
        self.assertEqual(code, 0)
        self.assertEqual(self._current(), self.older)
        # publish the newer one again, then --previous walks one step back in time
        code = cli.main(["rollback", "--publish-dir", str(self.release_dir), "--to", self.newer])
        self.assertEqual(code, 0)
        code = cli.main(["rollback", "--publish-dir", str(self.release_dir), "--previous"])
        self.assertEqual(code, 0)
        self.assertEqual(self._current(), self.older)
        # nothing earlier than the oldest release: refuse instead of moving
        code = cli.main(["rollback", "--publish-dir", str(self.release_dir), "--previous"])
        self.assertEqual(code, 2)
        self.assertEqual(self._current(), self.older)

    def test_web_cli_rollback_verifies(self) -> None:
        from randreport.web import __main__ as web_main

        code = web_main.main(["rollback", "--release-dir", str(self.release_dir), "--to", self.older])
        self.assertEqual(code, 0)
        self.assertEqual(self._current(), self.older)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
