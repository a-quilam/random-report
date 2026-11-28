"""A01 — Telegram login, sessions, CSRF, IDOR, XSS and no leak (docs/REQUIREMENTS.md §11, §13).

Everything runs against synthetic keys, an in-process OIDC provider and a
staging database built from the offline ingestion fixtures: no network, no real
Telegram account, no production service.
"""

from __future__ import annotations

import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from randreport.admin import app as admin_app
from randreport.admin import checks, jose, members, news, oidc, sessions
from randreport.store import connect
from tests import fixtures_admin as fa


class AdminTestCase(unittest.TestCase):
    """A private copy of the staging build, an allowlist and a desk."""

    def setUp(self) -> None:
        self.tmp = fa.admin_case_dir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "randreport.sqlite"
        self.releases = self.tmp / "releases"
        self.conn = connect(str(self.db))
        self.addCleanup(self.conn.close)
        members.ensure_bootstrap(self.conn, fa.ADMIN_ID)

    def desk(self, **overrides):
        options = {
            "db_path": self.db,
            "secret": fa.SERVER_SECRET,
            "release_dir": self.releases,
            "widget_bot_token": fa.BOT_TOKEN,
        }
        options.update(overrides)
        app = admin_app.AdminDesk(**options)
        self.addCleanup(app.close)
        return app

    def signed_in(self, *, user_id: str = fa.ADMIN_ID, provider=None, app=None):
        """Log in over the desk's own HTTP surface and return (client, response)."""

        provider = provider or fa.FakeOidcProvider(user_id=user_id)
        client = fa.WsgiClient(app or self.desk(oidc=fa.oidc_client(provider)))
        response = fa.login(client, provider)
        return client, response


class JoseTests(unittest.TestCase):
    def test_es256_and_rs256_tokens_verify(self) -> None:
        for alg in ("ES256", "RS256"):
            token = fa.sign_jwt(fa.claims_for(), alg=alg)
            verified = jose.verify_jwt(token, keys=jose.keys_from_jwks({"keys": [fa.es_jwk() if alg == "ES256" else fa.rs_jwk()]}), issuer=fa.ISSUER, audience=fa.CLIENT_ID)
            self.assertEqual(verified.claims["sub"], fa.ADMIN_ID)

    def test_none_and_symmetric_algorithms_are_refused(self) -> None:
        keys = jose.keys_from_jwks({"keys": [fa.es_jwk()]})
        for header in ({"alg": "none"}, {"alg": "HS256", "kid": fa.ES_KID}):
            segment = jose.b64url_encode(json.dumps(header).encode())
            payload = jose.b64url_encode(json.dumps(fa.claims_for()).encode())
            with self.assertRaises(jose.JoseError):
                jose.verify_jwt(f"{segment}.{payload}.{jose.b64url_encode(b'x')}", keys=keys)

    def test_tampered_signature_and_payload_are_refused(self) -> None:
        token = fa.sign_jwt(fa.claims_for())
        keys = jose.keys_from_jwks({"keys": [fa.es_jwk()]})
        header, payload, signature = token.split(".")
        with self.assertRaises(jose.JoseError):
            jose.verify_jwt(f"{header}.{payload}.{jose.b64url_encode(b'\\x00' * 64)}", keys=keys)
        forged = jose.b64url_encode(json.dumps(fa.claims_for(sub=fa.OUTSIDER_ID)).encode())
        with self.assertRaises(jose.JoseError):
            jose.verify_jwt(f"{header}.{forged}.{signature}", keys=keys)

    def test_claim_checks(self) -> None:
        keys = jose.keys_from_jwks({"keys": [fa.es_jwk()]})
        past = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        cases = {
            "issuer": {"iss": "https://evil.example"},
            "audience": {"aud": "another-client"},
            "expiry": {"exp": past},
            "nonce": {"nonce": "other"},
            "subject": {"sub": ""},
        }
        for name, override in cases.items():
            token = fa.sign_jwt(fa.claims_for(**override))
            with self.subTest(claim=name):
                with self.assertRaises(jose.JoseError):
                    jose.verify_jwt(token, keys=keys, issuer=fa.ISSUER, audience=fa.CLIENT_ID, nonce="expected")

    def test_short_or_unknown_keys_are_refused(self) -> None:
        with self.assertRaises(jose.JoseError):
            jose.key_from_jwk({"kty": "RSA", "kid": "short", "n": jose.b64url_encode(b"\x01" * 128), "e": "AQAB"})
        with self.assertRaises(jose.JoseError):
            jose.key_from_jwk({"kty": "oct", "kid": "symmetric", "k": "AAAA"})
        with self.assertRaises(jose.JoseError):
            jose.key_from_jwk({**fa.es_jwk(), "x": jose.b64url_encode(b"\x00" * 32)})


class OidcFlowTests(AdminTestCase):
    def test_login_grants_a_hardened_session_cookie(self) -> None:
        client, response = self.signed_in()
        self.assertEqual(response.status, 303)
        self.assertEqual(response.location, "/admin/")
        cookie = response.header("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Secure", cookie)
        raw = client.cookies[sessions.COOKIE_NAME]
        stored = self.conn.execute("SELECT session_hash FROM admin_sessions").fetchone()["session_hash"]
        self.assertNotEqual(stored, raw, "the raw cookie value must never be stored")
        self.assertEqual(len(stored), 64)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM admin_sessions").fetchone()["n"], 1)

    def test_username_spoof_does_not_grant_access(self) -> None:
        """A verified outsider cannot pass as a member by setting their username."""

        payload = fa.widget_payload(fa.OUTSIDER_ID, username="commune_test")
        client = fa.WsgiClient(self.desk())
        response = client.request("POST", "/admin/login/widget", form=payload)
        response.absorb_cookies()
        self.assertEqual(response.status, 403)
        self.assertNotIn(sessions.COOKIE_NAME, client.cookies)
        self.assertTrue(self.conn.execute("SELECT COUNT(*) AS n FROM admin_login_events WHERE outcome = 'denied'").fetchone()["n"])

    def test_widget_signature_and_freshness(self) -> None:
        app = self.desk()
        client = fa.WsgiClient(app)
        good = fa.widget_payload(fa.ADMIN_ID)
        response = client.request("POST", "/admin/login/widget", form=good)
        response.absorb_cookies()
        self.assertEqual(response.status, 303)
        self.assertIn("rd_admin_session", client.cookies)

        tampered = dict(fa.widget_payload(fa.ADMIN_ID))
        tampered["id"] = fa.OUTSIDER_ID
        other = fa.WsgiClient(app)
        response = other.request("POST", "/admin/login/widget", form=tampered)
        self.assertEqual(response.status, 401)
        self.assertIn('class="error"', response.text)
        self.assertNotIn("rd_admin_session", other.cookies)

        stale = fa.widget_payload(fa.ADMIN_ID, auth_date=str(int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp())))
        third = fa.WsgiClient(app)
        response = third.request("POST", "/admin/login/widget", form=stale)
        self.assertEqual(response.status, 401)
        self.assertIn('class="error"', response.text)

    def test_state_mismatch_and_forged_pending_cookie_are_refused(self) -> None:
        provider = fa.FakeOidcProvider()
        client = fa.WsgiClient(self.desk(oidc=fa.oidc_client(provider)))
        start = client.request("GET", "/admin/login")
        start.absorb_cookies()
        query = dict(chunk.split("=", 1) for chunk in start.location.split("?", 1)[1].split("&"))
        provider.nonce = query["nonce"]
        response = client.request("GET", "/admin/callback?code=x&state=not-the-state")
        self.assertEqual(response.status, 200)
        self.assertIn("state", response.text)
        self.assertNotIn(sessions.COOKIE_NAME, client.cookies)

        forged = fa.WsgiClient(self.desk(oidc=fa.oidc_client(provider)))
        response = forged.request("GET", "/admin/callback?code=x&state=y", headers={"COOKIE": f"{oidc.PENDING_COOKIE}=eyJhIjoxfQ.deadbeef"})
        self.assertEqual(response.status, 200)
        self.assertIn('class="error"', response.text)
        self.assertNotIn(sessions.COOKIE_NAME, forged.cookies)

    def test_expired_and_unconfirmed_tokens_are_refused(self) -> None:
        past = int((datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp())
        provider = fa.FakeOidcProvider(claims=fa.claims_for(exp=past))
        client = fa.WsgiClient(self.desk(oidc=fa.oidc_client(provider)))
        response = fa.login(client, provider)
        self.assertEqual(response.status, 200)
        self.assertIn("expired", response.text)
        self.assertNotIn(sessions.COOKIE_NAME, client.cookies)

    def test_unknown_key_id_refreshes_the_jwks_once(self) -> None:
        provider = fa.FakeOidcProvider()
        foreign = dict(fa.es_jwk())
        foreign["kid"] = "rotated-key-0"
        provider.jwks_responses = [{"keys": [foreign]}, {"keys": [fa.es_jwk()]}]
        desk = self.desk(oidc=fa.oidc_client(provider))
        client = fa.WsgiClient(desk)
        start = client.request("GET", "/admin/login")
        start.absorb_cookies()
        query = dict(chunk.split("=", 1) for chunk in start.location.split("?", 1)[1].split("&"))
        provider.nonce = query["nonce"]

        response = client.request("GET", f"/admin/callback?code=x&state={query['state']}")
        response.absorb_cookies()
        self.assertEqual(response.status, 303)
        self.assertEqual([url for _method, url in provider.requests].count(f"{fa.ISSUER}/jwks"), 2)

    def test_insecure_urls_and_unlisted_redirects_are_refused(self) -> None:
        provider = fa.FakeOidcProvider()
        with self.assertRaises(oidc.InsecureUrl):
            fa.oidc_client(provider, issuer="http://oauth.telegram.test")
        with self.assertRaises(oidc.InsecureUrl):
            fa.oidc_client(provider, redirect_uri="http://desk.example/admin/callback")
        with self.assertRaises(oidc.AuthError):
            fa.oidc_client(provider, redirect_uri="https://other.example/admin/callback", allowed_redirect_uris=(fa.REDIRECT_URI,))
        loopback = fa.oidc_client(provider, issuer="http://127.0.0.1:9", redirect_uri="http://127.0.0.1:9/admin/callback", allow_insecure_loopback=True, allowed_redirect_uris=("http://127.0.0.1:9/admin/callback",))
        self.assertEqual(loopback.issuer, "http://127.0.0.1:9")

    def test_pkce_challenge_is_s256_and_the_verifier_is_sent(self) -> None:
        provider = fa.FakeOidcProvider()
        client = fa.WsgiClient(self.desk(oidc=fa.oidc_client(provider)))
        start = client.request("GET", "/admin/login")
        query = dict(chunk.split("=", 1) for chunk in start.location.split("?", 1)[1].split("&"))
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertIn("openid", query["scope"])
        self.assertNotIn("phone", query["scope"])
        start.absorb_cookies()
        provider.nonce = query["nonce"]
        client.request("GET", f"/admin/callback?code=x&state={query['state']}")
        self.assertTrue(provider.code_verifier_seen)
        self.assertEqual(jose.b64url_encode(__import__("hashlib").sha256(provider.code_verifier_seen.encode()).digest()), query["code_challenge"])


class AllowlistTests(AdminTestCase):
    def test_deny_by_default_and_bootstrap(self) -> None:
        conn = connect(str(self.tmp / "fresh.sqlite"))
        self.addCleanup(conn.close)
        from randreport.store import apply_migrations

        apply_migrations(conn)
        self.assertFalse(members.is_member(conn, fa.ADMIN_ID))
        self.assertTrue(members.ensure_bootstrap(conn, fa.ADMIN_ID))
        self.assertTrue(members.is_member(conn, fa.ADMIN_ID))
        self.assertFalse(members.ensure_bootstrap(conn, fa.SECOND_ADMIN_ID))
        self.assertEqual(len(members.active_members(conn)), 1)

    def test_bootstrap_does_not_reopen_a_revoked_desk(self) -> None:
        members.add_member(self.conn, fa.SECOND_ADMIN_ID)
        members.revoke_member(self.conn, fa.ADMIN_ID)
        self.assertFalse(members.ensure_bootstrap(self.conn, fa.OUTSIDER_ID))
        self.assertEqual([member.telegram_user_id for member in members.active_members(self.conn)], [fa.SECOND_ADMIN_ID])

    def test_last_member_cannot_be_removed(self) -> None:
        with self.assertRaises(members.MemberError):
            members.revoke_member(self.conn, fa.ADMIN_ID)

    def test_revocation_kills_the_session_immediately(self) -> None:
        members.add_member(self.conn, fa.SECOND_ADMIN_ID)
        client, _response = self.signed_in()
        self.assertEqual(client.request("GET", "/admin/").status, 200)
        members.revoke_member(self.conn, fa.ADMIN_ID, actor=fa.SECOND_ADMIN_ID)
        self.assertEqual(client.request("GET", "/admin/").status, 303)
        row = self.conn.execute("SELECT revoked_at, revoked_reason FROM admin_sessions").fetchone()
        self.assertIsNotNone(row["revoked_at"])
        self.assertEqual(row["revoked_reason"], "removed")

    def test_revoked_member_cannot_log_in_again(self) -> None:
        members.add_member(self.conn, fa.SECOND_ADMIN_ID)
        members.revoke_member(self.conn, fa.SECOND_ADMIN_ID)
        _client, response = self.signed_in(user_id=fa.SECOND_ADMIN_ID)
        self.assertEqual(response.status, 403)

    def test_membership_change_is_audited(self) -> None:
        members.add_member(self.conn, fa.SECOND_ADMIN_ID, actor=fa.ADMIN_ID, note="новый коммунар")
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("admin.member.added", actions)
        members.revoke_member(self.conn, fa.SECOND_ADMIN_ID, actor=fa.ADMIN_ID)
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("admin.member.revoked", actions)

    def test_the_desk_tables_come_from_a_pending_migration(self) -> None:
        from randreport.store import apply_migrations, connect as open_db

        conn = open_db(str(self.tmp / "schema.sqlite"))
        applied = apply_migrations(conn)
        self.assertIn(3, applied)
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in (
            "admin_sessions",
            "admin_login_events",
            "news_revisions",
            "news_merges",
            "post_drafts",
            "finance_annotations",
            "check_invalidations",
        ):
            self.assertIn(table, names)
        conn.close()


class SessionAndCsrfTests(AdminTestCase):
    def test_session_expiry_revocation_and_rotation(self) -> None:
        session = sessions.create_session(self.conn, user_id=fa.ADMIN_ID, display_name="Тест")
        self.assertIsNotNone(sessions.load_session(self.conn, session.session_id))
        self.assertIsNone(sessions.load_session(self.conn, session.session_id, now=datetime.now(timezone.utc) + timedelta(days=2)))
        rotated = sessions.rotate(self.conn, session.session_id)
        self.assertIsNotNone(rotated)
        self.assertIsNone(sessions.load_session(self.conn, session.session_id))
        self.assertIsNotNone(sessions.load_session(self.conn, rotated.session_id))
        self.assertTrue(sessions.revoke(self.conn, rotated.session_id, reason="logout"))
        self.assertIsNone(sessions.load_session(self.conn, rotated.session_id))

    def test_csrf_token_is_required_and_origin_is_checked(self) -> None:
        client, _response = self.signed_in()
        page = client.request("GET", "/admin/reconciliation")
        token = fa.csrf_from(page)
        ok = client.request(
            "POST",
            "/admin/actions/verify",
            form={"csrf": token, "entity_type": "period", "entity_key": "2025-07"},
            headers={"ORIGIN": fa.ORIGIN},
        )
        self.assertEqual(ok.status, 303)
        self.assertEqual(checks.effective_state(self.conn, "period", "2025-07"), checks.VERIFIED)

        missing = client.request("POST", "/admin/actions/verify", form={"entity_type": "period", "entity_key": "2025-07"}, headers={"ORIGIN": fa.ORIGIN})
        self.assertEqual(missing.status, 403)
        wrong = client.request("POST", "/admin/actions/verify", form={"csrf": "nope", "entity_type": "period", "entity_key": "2025-07"}, headers={"ORIGIN": fa.ORIGIN})
        self.assertEqual(wrong.status, 403)
        foreign = client.request("POST", "/admin/actions/verify", form={"csrf": token, "entity_type": "period", "entity_key": "2025-07"}, headers={"ORIGIN": "https://evil.example"})
        self.assertEqual(foreign.status, 403)
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("admin.csrf.rejected", actions)

    def test_login_is_rate_limited(self) -> None:
        desk = self.desk(widget_bot_token=fa.BOT_TOKEN, rate_limit_attempts=3)
        client = fa.WsgiClient(desk)
        tampered = dict(fa.widget_payload(fa.OUTSIDER_ID))
        tampered["first_name"] = "spoiled"
        for _ in range(3):
            sessions.record_login(self.conn, subject=fa.OUTSIDER_ID, outcome="denied", reason="test")
        response = client.request("POST", "/admin/login/widget", form=fa.widget_payload(fa.OUTSIDER_ID))
        self.assertEqual(response.status, 429)
        self.assertIn("Слишком много", response.text)

    def test_logout_revokes_and_clears_the_cookie(self) -> None:
        client, _response = self.signed_in()
        token = fa.csrf_from(client.request("GET", "/admin/"))
        response = client.request("POST", "/admin/logout", form={"csrf": token}, headers={"ORIGIN": fa.ORIGIN})
        response.absorb_cookies()
        self.assertEqual(response.status, 303)
        self.assertNotIn(sessions.COOKIE_NAME, client.cookies)
        self.assertEqual(client.request("GET", "/admin/").status, 303)
        self.assertIsNotNone(self.conn.execute("SELECT revoked_at FROM admin_sessions").fetchone()["revoked_at"])


class AccessControlTests(AdminTestCase):
    def test_anonymous_cannot_read_any_desk_page(self) -> None:
        client = fa.WsgiClient(self.desk())
        for path in ("/admin/", "/admin/money", "/admin/news", "/admin/reconciliation", "/admin/publish", "/admin/settings", "/admin/audit"):
            with self.subTest(path=path):
                response = client.request("GET", path)
                self.assertEqual(response.status, 303)
                self.assertEqual(response.location, "/admin/login")

    def test_unknown_and_private_entities_are_not_found(self) -> None:
        client, _response = self.signed_in()
        for path in ("/admin/money/tx_private", "/admin/money/tx_mixed", "/admin/money/tx_does_not_exist", "/admin/publish/1999-01/v3", "/admin/nope"):
            with self.subTest(path=path):
                self.assertEqual(client.request("GET", path).status, 404)

    def test_out_of_contour_operations_are_never_listed(self) -> None:
        client, _response = self.signed_in()
        page = client.request("GET", "/admin/money")
        self.assertNotIn("tx_private", page.text)
        self.assertNotIn("tx_mixed", page.text)
        self.assertIn("tx_rent", page.text)

    def test_money_fields_are_read_only(self) -> None:
        client, _response = self.signed_in()
        token = fa.csrf_from(client.request("GET", "/admin/money"))
        response = client.request(
            "POST",
            "/admin/actions/money/edit",
            form={"csrf": token, "transaction_id": "tx_rent", "amount": "1"},
            headers={"ORIGIN": fa.ORIGIN},
        )
        self.assertEqual(response.status, 409)
        self.assertIn("только для чтения", response.text)
        before = self.conn.execute("SELECT amount_kopecks FROM finance_transactions WHERE transaction_id = 'tx_rent'").fetchone()["amount_kopecks"]
        self.assertEqual(before, 4_000_000)

    def test_annotation_is_allowed_and_audited(self) -> None:
        client, _response = self.signed_in()
        token = fa.csrf_from(client.request("GET", "/admin/money"))
        response = client.request(
            "POST",
            "/admin/actions/money/annotate",
            form={"csrf": token, "transaction_id": "tx_rent", "note": "оплата за июль по договору"},
            headers={"ORIGIN": fa.ORIGIN},
        )
        self.assertEqual(response.status, 303)
        row = self.conn.execute("SELECT note, actor FROM finance_annotations WHERE transaction_id = 'tx_rent'").fetchone()
        self.assertEqual(row["note"], "оплата за июль по договору")
        self.assertEqual(row["actor"], fa.ADMIN_ID)

    def test_the_overview_links_to_a_source_detail_page(self) -> None:
        client, _response = self.signed_in()
        overview = client.request("GET", "/admin/")
        self.assertIn("/admin/source/zenmoney", overview.text)
        detail = client.request("GET", "/admin/source/zenmoney")
        self.assertEqual(detail.status, 200)
        self.assertIn("Запуски", detail.text)
        self.assertIn("Последний проверенный снимок", detail.text)
        self.assertIn("устарело", detail.text)
        self.assertEqual(client.request("GET", "/admin/source/not_a_source").status, 404)

    def test_settings_actions_wire_news_sources_and_donor_aliases(self) -> None:
        client, _response = self.signed_in()
        token = fa.csrf_from(client.request("GET", "/admin/settings"))
        response = client.request(
            "POST",
            "/admin/actions/news-source",
            form={"csrf": token, "source_id": "main_channel", "channel_id": "@random_culture", "enabled": "да"},
            headers={"ORIGIN": fa.ORIGIN},
        )
        self.assertEqual(response.status, 303)
        response = client.request(
            "POST",
            "/admin/actions/donor-alias",
            form={"csrf": token, "alias_id": "alias_1", "public_label": "Псевдоним из отчёта", "allowed": "да", "note": "уже публиковался"},
            headers={"ORIGIN": fa.ORIGIN},
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM news_sources").fetchone()["n"], 1)
        aliases = self.conn.execute("SELECT public_label, allowed FROM donor_public_aliases").fetchall()
        self.assertEqual([(row["public_label"], row["allowed"]) for row in aliases], [("Псевдоним из отчёта", 1)])
        actions = {row["action"] for row in self.conn.execute("SELECT action FROM audit_log")}
        self.assertIn("settings.news_source", actions)
        self.assertIn("settings.donor_alias", actions)

    def test_mapping_rollback_is_a_new_version_never_a_rewrite(self) -> None:
        client, _response = self.signed_in()
        token = fa.csrf_from(client.request("GET", "/admin/settings"))
        first = client.request(
            "POST",
            "/admin/actions/mapping/category",
            form={"csrf": token, "category_id": "cat_rent", "label": "Аренда зала"},
            headers={"ORIGIN": fa.ORIGIN},
        )
        self.assertEqual(first.status, 303)
        versions = [row["version"] for row in self.conn.execute("SELECT version FROM category_mapping_versions ORDER BY created_at, version")]
        self.assertGreaterEqual(len(versions), 2)
        rolled = client.request(
            "POST",
            "/admin/actions/mapping/rollback",
            form={"csrf": token, "kind": "category", "version": versions[-2]},
            headers={"ORIGIN": fa.ORIGIN},
        )
        self.assertEqual(rolled.status, 303)
        after = [row["version"] for row in self.conn.execute("SELECT version FROM category_mapping_versions ORDER BY created_at, version")]
        self.assertGreater(len(after), len(versions))
        self.assertIn(versions[-2], after)


class InjectionAndLeakTests(AdminTestCase):
    def test_stored_markup_is_escaped_everywhere(self) -> None:
        attack = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        row = self.conn.execute("SELECT news_id FROM news_items LIMIT 1").fetchone()
        news_id = str(row["news_id"])
        news.edit(self.conn, news_id=news_id, actor=fa.ADMIN_ID, title=attack, summary='" onmouseover="alert(3)')
        client, _response = self.signed_in()
        news_page = client.request("GET", "/admin/news")
        self.assertNotIn("<script>alert(1)</script>", news_page.text)
        self.assertNotIn("<img src=x", news_page.text)
        self.assertNotIn("onmouseover=\"alert(3)\"", news_page.text)
        self.assertIn("&lt;script&gt;", news_page.text)
        self.assertIn("&quot;", news_page.text)
        reconciliation = client.request("GET", "/admin/reconciliation")
        self.assertNotIn("<script>", reconciliation.text)
        self.assertNotIn("<script", client.request("GET", "/admin/money?q=<script>alert(1)</script>").text)

    def test_notice_reflection_is_escaped(self) -> None:
        client, _response = self.signed_in()
        page = client.request("GET", "/admin/news?notice=<script>alert(1)</script>")
        self.assertEqual(page.status, 200)
        self.assertNotIn("<script>alert(1)</script>", page.text)

    def test_desk_pages_have_no_javascript_no_indexing_and_no_store(self) -> None:
        client, _response = self.signed_in()
        page = client.request("GET", "/admin/")
        self.assertNotIn("<script", page.text.lower())
        self.assertNotIn("onclick=", page.text.lower())
        self.assertIn("noindex", page.header("X-Robots-Tag"))
        self.assertIn("no-store", page.header("Cache-Control"))
        csp = page.header("Content-Security-Policy")
        self.assertIn("script-src 'none'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertEqual(client.request("GET", "/admin/healthz").status, 200)

    def test_export_carries_no_private_identifiers_or_markup(self) -> None:
        client, _response = self.signed_in()
        response = client.request("GET", "/admin/publish/2025-07/export")
        self.assertEqual(response.status, 200)
        body = response.text
        for token in ("tx_", "acc_", "cat_", "occ_", "pair_", "account_id", "<script", "<a "):
            self.assertNotIn(token, body)
        self.assertIn("Отчёт за 2025-07", body)

    def test_error_pages_carry_no_traceback_or_token(self) -> None:
        client = fa.WsgiClient(self.desk())
        response = client.request("POST", "/admin/actions/verify", form={"csrf": "x"})
        self.assertIn(response.status, (403, 404))
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("secret", response.text.lower())

    def test_healthz_needs_no_auth_and_exposes_nothing(self) -> None:
        response = fa.WsgiClient(self.desk()).request("GET", "/admin/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text), {"status": "ok"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
