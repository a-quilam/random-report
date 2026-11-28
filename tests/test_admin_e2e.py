"""End-to-end: sync → public → verify → edit → archive → correct → restore.

The whole chain runs over **real HTTP** on loopback: two WSGI servers (the public
site and the closed desk) in threads, a cookie-keeping client that does not
follow redirects, and a staging database built by the real nightly cycle from the
offline fixtures. Nothing here touches the network, a paid service or a
production host.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import shutil
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from randreport.admin import app as admin_app
from randreport.admin import checks
from randreport.store import connect
from randreport.web.app import PublicSite
from tests import fixtures_admin as fa


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):  # noqa: D102 - silence the test output
        pass


def _serve(app) -> tuple[str, WSGIServer]:
    server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}", server


class _Http:
    """A plain HTTP client that never follows a redirect (so Location is visible)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            _NoRedirect(),
        )

    def request(self, method: str, path: str, data: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
        body = None if data is None else urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(f"{self.base}{path}", data=body, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, dict(exc.headers), exc.read().decode("utf-8")
            finally:
                exc.close()  # an HTTPError holds an open response object

    def cookie(self, name: str) -> str | None:
        for cookie in self.jar:
            if cookie.name == name:
                return cookie.value
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def csrf(text: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', text)
    if match is None:
        raise AssertionError("no CSRF token in the page")
    return match.group(1)


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = fa.admin_case_dir(prefix="randreport-e2e-")
        cls.db = cls.root / "randreport.sqlite"
        cls.releases = cls.root / "releases"
        cls.provider = fa.FakeOidcProvider()
        cls.desk = admin_app.AdminDesk(
            db_path=cls.db,
            secret=fa.SERVER_SECRET,
            oidc=fa.oidc_client(cls.provider),
            release_dir=cls.releases,
            widget_bot_token=fa.BOT_TOKEN,
            # staging runs plain HTTP on loopback, where a browser (and a real
            # client) would not send a Secure cookie; production keeps Secure on
            # (asserted in the A01 suite over the WSGI test client).
            secure_cookies=False,
        )
        cls.site = PublicSite(release_dir=cls.releases, db_path=cls.db)
        cls.public_base, cls.public_server = _serve(cls.site)
        cls.desk_base, cls.desk_server = _serve(cls.desk)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.public_server.shutdown()
        cls.desk_server.shutdown()
        cls.public_server.server_close()
        cls.desk_server.server_close()
        cls.desk.close()
        cls.site.close()
        shutil.rmtree(cls.root, ignore_errors=True)

    # --- helpers ----------------------------------------------------------- #

    def login(self) -> tuple[_Http, str]:
        conn = connect(str(self.db))
        self.addCleanup(conn.close)
        from randreport.admin import members

        members.ensure_bootstrap(conn, fa.ADMIN_ID)
        client = _Http(self.desk_base)
        status, headers, _body = client.request("GET", "/admin/login")
        self.assertEqual(status, 303)
        authorization = headers["Location"]
        query = dict(chunk.split("=", 1) for chunk in authorization.split("?", 1)[1].split("&"))
        self.provider.nonce = query["nonce"]
        status, headers, body = client.request("GET", f"/admin/callback?code=synthetic-code&state={query['state']}")
        self.assertEqual(status, 303, body)
        self.assertEqual(headers["Location"], "/admin/")
        self.assertIsNotNone(client.cookie("rd_admin_session"))
        return client, authorization

    # --- the chain --------------------------------------------------------- #

    def test_full_chain_over_real_http(self) -> None:
        # 1. the nightly cycle already ran: the public site serves the live release
        public = _Http(self.public_base)
        status, _headers, money = public.request("GET", "/money?period=2025-07")
        self.assertEqual(status, 200)
        self.assertIn("19", money)
        status, _headers, api = public.request("GET", "/api/public/money?period=2025-07")
        self.assertEqual(status, 200)
        release = json.loads(api)
        expenses = release["metrics"]["expense_kopecks"]
        self.assertGreater(expenses, 0)
        release_id = release["release_id"]

        # 2. the desk is closed: no page without a session, and no provider redirect leakage
        anonymous = _Http(self.desk_base)
        status, headers, _body = anonymous.request("GET", "/admin/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/login")

        # 3. login through the real OIDC redirect (fake provider, real HTTP)
        desk, authorization = self.login()
        self.assertIn("/auth?", authorization)
        status, _headers, overview = desk.request("GET", "/admin/")
        self.assertEqual(status, 200)
        self.assertIn("Обзор", overview)
        self.assertIn(release_id, overview)

        # the desk states the same figure the public API serves for the live plane
        status, _headers, publish_page = desk.request("GET", "/admin/publish/2025-07")
        self.assertEqual(status, 200)
        expected_rubles = f"{expenses // 100:,}".replace(",", "\u00a0") + ",00 ₽"
        self.assertIn(expected_rubles, publish_page)

        # 4. verify the period from the desk, with the CSRF token from the page
        status, _headers, reconciliation = desk.request("GET", "/admin/reconciliation")
        self.assertEqual(status, 200)
        token = csrf(reconciliation)
        status, headers, body = desk.request(
            "POST",
            "/admin/actions/verify",
            data={"csrf": token, "entity_type": "period", "entity_key": "2025-07"},
        )
        self.assertEqual(status, 303, body)
        conn = connect(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(checks.effective_state(conn, "period", "2025-07"), checks.VERIFIED)

        # a foreign origin is refused by the same endpoint
        foreign = _Http(self.desk_base)
        for cookie in desk.jar:
            foreign.jar.set_cookie(cookie)
        status, _headers, body = foreign.request(
            "POST",
            "/admin/actions/verify",
            data={"csrf": token, "entity_type": "period", "entity_key": "2025-07"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("чужого источника", body)

        # 5. edit the chronicle news and hide the announcement-like duplicate
        news_id = str(conn.execute("SELECT news_id FROM news_items LIMIT 1").fetchone()["news_id"])
        status, _headers, body = desk.request(
            "POST",
            "/admin/actions/news/edit",
            data={"csrf": token, "news_id": news_id, "title": "Отремонтировали вход в подвал (правка)", "summary": "Проверено редакцией."},
        )
        self.assertEqual(status, 303, body)
        row = conn.execute("SELECT title, manual_state FROM news_items WHERE news_id = ?", (news_id,)).fetchone()
        self.assertEqual(row["manual_state"], "edited")
        self.assertIn("правка", row["title"])

        # 6. archive: close the period, then correct it — a new version, never a rewrite
        status, _headers, body = desk.request(
            "POST",
            "/admin/actions/publish/close",
            data={"csrf": token, "period": "2025-07", "reason": "месяц закрыт редакцией"},
        )
        self.assertEqual(status, 303, body)
        status, _headers, body = desk.request(
            "POST",
            "/admin/actions/publish/correct",
            data={"csrf": token, "period": "2025-07", "field": "events", "value": "7", "reason": "нашлось седьмое мероприятие"},
        )
        self.assertEqual(status, 303, body)
        versions = conn.execute("SELECT version FROM report_snapshots WHERE period_label = '2025-07' ORDER BY version").fetchall()
        self.assertEqual([int(item["version"]) for item in versions], [1, 2])

        # 7. the public site shows the frozen versions and never the desk
        status, _headers, report_page = public.request("GET", "/reports/2025-07")
        self.assertEqual(status, 200)
        self.assertIn("/reports/2025-07/v1", report_page)
        self.assertIn("нашлось седьмое мероприятие", report_page)
        status, _headers, _body = public.request("GET", "/admin/")
        self.assertEqual(status, 404)
        self.assertNotIn("rd_admin", report_page)

        # 8. the desk export is Markdown without any private identifier
        status, headers, markdown = desk.request("GET", "/admin/publish/2025-07/export")
        self.assertEqual(status, 200)
        for token_to_avoid in ("tx_", "acc_", "pair_", "<"):
            self.assertNotIn(token_to_avoid, markdown)
        self.assertIn("Отчёт за 2025-07", markdown)

        # 9. restore: a consistent SQLite backup keeps the frozen version and the manual edit
        restored_root = self.root / "restored"
        restored_root.mkdir(exist_ok=True)
        source = connect(str(self.db))
        target = connect(str(restored_root / "randreport.sqlite"))
        source.backup(target)  # the documented consistent backup API, not a file copy
        target.commit()
        self.addCleanup(target.close)
        self.addCleanup(source.close)
        shutil.copytree(self.releases, restored_root / "releases", dirs_exist_ok=True, symlinks=True)
        versions = target.execute("SELECT version, payload FROM report_snapshots WHERE period_label = '2025-07' ORDER BY version").fetchall()
        self.assertEqual([int(item["version"]) for item in versions], [1, 2])
        self.assertIn("corrections", versions[1]["payload"])
        title = target.execute("SELECT title FROM news_items WHERE news_id = ?", (news_id,)).fetchone()["title"]
        self.assertIn("правка", title)
        restored_site = PublicSite(release_dir=restored_root / "releases", db_path=restored_root / "randreport.sqlite")
        base, server = _serve(restored_site)
        try:
            status, _headers, page = _Http(base).request("GET", "/reports/2025-07")
            self.assertEqual(status, 200)
            self.assertIn("нашлось седьмое мероприятие", page)
        finally:
            server.shutdown()
            server.server_close()
            restored_site.close()

        # 10. logging out revokes the session for real HTTP as well
        status, headers, _body = desk.request("POST", "/admin/logout", data={"csrf": token})
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        status, headers, _body = desk.request("GET", "/admin/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/login")

    def test_the_two_planes_are_not_mixed_on_the_desk_page(self) -> None:
        desk, _authorization = self.login()
        status, _headers, page = desk.request("GET", "/admin/publish/2025-07")
        self.assertEqual(status, 200)
        self.assertIn("Плоскость публикации", page)
        self.assertIn("живой выпуск", page)
        status, _headers, historical = desk.request("GET", "/admin/publish/2025-01")
        self.assertEqual(status, 200)
        self.assertIn("исторический", historical.lower())
        self.assertNotIn("999 999", historical)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
