"""The closed editorial desk: an authenticated WSGI application (sections 4, 11).

Every request passes the same gate before any desk data is read:

1. a session cookie whose hash exists, is unexpired and is not revoked;
2. a membership re-check against the allowlist (deny by default);
3. for every state-changing request, a CSRF token bound to the session *and* an
   ``Origin``/``Referer`` check when the browser sends one.

The desk has no JavaScript (its CSP allows none), reads no secret from the
environment at request time, serves no public route, forbids indexing and sends
``no-store`` on every response. Errors carry a human sentence, never a traceback,
a token or an identifier of an unrelated entity.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..store import apply_migrations, connect
from ..store import repository as repo
from . import checks, desk, diagnostics, history_admin, members, news, oidc, render, reports, sessions

ADMIN_ROOT = "/admin"
MAX_BODY = 64 * 1024

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'none'; connect-src 'none'; "
    "script-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
    ("X-Robots-Tag", "noindex, nofollow, noarchive"),
    ("Content-Security-Policy", CSP),
    ("Cache-Control", "no-store, no-cache, must-revalidate, private"),
)

STATUS_TEXT = {
    200: "200 OK",
    303: "303 See Other",
    400: "400 Bad Request",
    401: "401 Unauthorized",
    403: "403 Forbidden",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    409: "409 Conflict",
    413: "413 Payload Too Large",
    429: "429 Too Many Requests",
    500: "500 Internal Server Error",
}


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


class AdminDesk:
    """WSGI application for the closed editorial desk."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        secret: str | bytes,
        oidc: oidc.TelegramOidc | None = None,
        widget_bot_token: str | None = None,
        release_dir: str | Path | None = None,
        static_dir: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
        secure_cookies: bool = True,
        migrate: bool = True,
        rate_limit_attempts: int = sessions.MAX_LOGIN_ATTEMPTS,
    ) -> None:
        if isinstance(secret, str) and len(secret) < 16:
            raise ValueError("the desk needs a server secret of at least 16 characters")
        self.db_path = Path(db_path)
        self.secret = secret
        self.oidc = oidc
        self.widget_bot_token = widget_bot_token
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).with_name("static")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.secure_cookies = secure_cookies
        self.migrate = migrate
        self.rate_limit_attempts = rate_limit_attempts
        self._conn: sqlite3.Connection | None = None
        self._release_dir = Path(release_dir) if release_dir else None
        self._index: Any = None

    # --- infrastructure ---------------------------------------------------- #

    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            # the desk is a long-lived WSGI app: it is created lazily on the
            # request thread and closed from the main thread at shutdown, so the
            # per-thread guard would turn that close() into a leaked handle
            conn = connect(self.db_path, check_same_thread=False)
            if self.migrate:
                apply_migrations(conn)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:  # pragma: no cover - closed from another thread at shutdown
                pass
            finally:
                self._conn = None

    def _release_index(self) -> Any:
        if self._release_dir is None:
            return None
        if self._index is None:
            from ..web.release_index import ReleaseIndex

            self._index = ReleaseIndex(self._release_dir)
        return self._index

    def now(self) -> datetime:
        return self._now()

    # --- WSGI -------------------------------------------------------------- #

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        try:
            response = self._dispatch(environ)
        except Exception:  # noqa: BLE001 - never leak a traceback to the desk
            response = self._error(500, "Внутренняя ошибка редакционного стола.")
        headers = list(response.headers)
        if not any(name.lower() == "content-type" for name, _ in headers):
            headers.insert(0, ("Content-Type", response.content_type))
        start_response(STATUS_TEXT.get(response.status, f"{response.status}"), headers)
        return [response.body]

    def _dispatch(self, environ: Mapping[str, Any]) -> Response:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/") or "/")
        query = _parse_query(environ.get("QUERY_STRING", ""))
        if not path.startswith(ADMIN_ROOT):
            return self._error(404, "Адрес не найден.", public=False)
        tail = path[len(ADMIN_ROOT):] or "/"
        if tail == "/healthz":
            return self._json({"status": "ok"})
        if tail.startswith("/static/"):
            return self._static(tail)
        if method not in ("GET", "HEAD", "POST"):
            return self._error(405, "Метод не поддерживается.")

        conn = self.connection()
        if method == "POST":
            return self._post(conn, tail, environ)
        return self._get(conn, tail, query, environ)

    # --- GET --------------------------------------------------------------- #

    def _get(self, conn: sqlite3.Connection, tail: str, query: Mapping[str, str], environ: Mapping[str, Any]) -> Response:
        if tail in ("/login",):
            return self._login(conn, environ)
        if tail == "/callback":
            return self._callback(conn, query, environ)
        session = self._session(conn, environ)
        if session is None:
            return self._redirect(f"{ADMIN_ROOT}/login")
        notice = str(query.get("notice") or "")
        actor = self._actor_name(session)
        if tail in ("", "/"):
            findings = diagnostics.collect(conn, now=self.now())
            diagnostics.sync_issues(conn, findings, now=self.now().isoformat())
            data = desk.overview(conn, index=self._release_index(), now=self.now())
            body = render.overview_panel(data)
            title = "Обзор"
        elif tail == "/money":
            rows = desk.money_rows(conn, period=query.get("p") or None, text=query.get("q") or None)
            body = render.money_panel(rows, query=query)
            title = "Деньги"
        elif tail.startswith("/money/"):
            transaction_id = tail[len("/money/"):]
            card = desk.money_card(conn, transaction_id)
            if card is None:
                return self._error(404, "Операция не найдена в публичном контуре.")
            body = render.money_card_panel(
                card,
                csrf=session.csrf_token,
                excluded=desk.is_operation_excluded(conn, transaction_id),
            )
            title = "Операция"
        elif tail.startswith("/source/"):
            source = tail[len("/source/"):]
            detail = desk.source_detail(conn, source, now=self.now())
            if not detail["exists"]:
                return self._error(404, "Источник не найден.")
            body = render.source_detail_panel(detail)
            title = f"Источник {source}"
            page = render.layout(title=title, active="/admin/", body=body, actor=actor, csrf=session.csrf_token, notice=notice)
            return Response(200, "text/html; charset=utf-8", page.encode("utf-8"), (("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS))
        elif tail == "/events":
            body = render.events_panel(desk.events_rows(conn), csrf=session.csrf_token)
            title = "События"
        elif tail == "/channel":
            body = render.channel_panel(desk.channel_rows(conn))
            title = "Канал"
        elif tail == "/news":
            news.reconcile_merges(conn)
            public_ids = [item["news_id"] for item in news.public_items(conn)]
            body = render.news_panel(news.desk_items(conn), csrf=session.csrf_token, public_ids=public_ids, notice=notice)
            title = "Новости"
        elif tail == "/reconciliation":
            findings = diagnostics.collect(conn, now=self.now())
            diagnostics.sync_issues(conn, findings, now=self.now().isoformat())
            issues_rows = []
            for row in diagnostics.issues(conn):
                item = dict(row)
                item["comment"] = diagnostics.comment_of(row)
                issues_rows.append(item)
            body = render.reconciliation_panel(
                [_finding_view(finding) for finding in findings], issues_rows, csrf=session.csrf_token
            )
            title = "Сверка"
        elif tail == "/publish":
            body = render.publish_panel(periods=_period_index(conn), csrf=session.csrf_token, history_html=render.history_section())
            title = "Публикация"
        elif tail.startswith("/publish/"):
            return self._publish(conn, tail[len("/publish/"):], session=session)
        elif tail == "/settings":
            body = render.settings_panel(
                desk.settings_view(conn, now=self.now()),
                desk.audit_trail(conn),
                desk.invalidations(conn),
                csrf=session.csrf_token,
                allocation=desk.allocation_view(conn),
            )
            title = "Настройки"
        elif tail == "/audit":
            body = render.audit_panel(desk.audit_trail(conn, limit=200), desk.invalidations(conn, limit=200))
            title = "Журнал"
        else:
            return self._error(404, "Страница не найдена.")
        page = render.layout(title=title, active=_active(tail), body=body, actor=actor, csrf=session.csrf_token, notice=notice)
        headers = (("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS)
        return Response(200, "text/html; charset=utf-8", page.encode("utf-8"), headers)

    def _publish(self, conn: sqlite3.Connection, tail: str, *, session: sessions.Session) -> Response:
        parts = [part for part in tail.split("/") if part]
        if not parts:
            return self._error(404, "Период не указан.")
        label = parts[0]
        if len(parts) == 1:
            try:
                built = reports.build(conn, label=label)
                state = reports.publication_state(conn, label)
                planes = reports.planes(conn, self._release_index(), label)
            except (reports.PublicationError, reports.reports_module.ReportError) as exc:
                return self._error(409, str(exc))
            body = render.publish_panel(
                periods=_period_index(conn),
                csrf=session.csrf_token,
                selected=built,
                planes=planes,
                state=state,
                history_html=render.history_section(),
            )
            return self._html(body, title=f"Публикация {label}", session=session)
        if parts[1] == "export":
            try:
                markdown = reports.export_markdown(conn, label=label)
            except (reports.PublicationError, reports.reports_module.ReportError) as exc:
                return self._error(409, str(exc))
            headers = (
                ("Content-Disposition", f'attachment; filename="report-{label}.md"'),
                *SECURITY_HEADERS,
            )
            return Response(200, "text/markdown; charset=utf-8", markdown.encode("utf-8"), headers)
        if parts[1].startswith("v") and parts[1][1:].isdigit():
            version = int(parts[1][1:])
            try:
                text = reports.text_for_version(conn, label, version)
            except reports.reports_module.ReportError as exc:
                return self._error(409, str(exc))
            if text is None:
                return self._error(404, "Версия отчёта не найдена.")
            body = (
                f'<h1>Версия {version} отчёта за {render.e(label)}</h1>'
                f'<pre class="draft">{render.e(text)}</pre>'
                f'<p><a href="/admin/publish/{render.e(label)}">К черновику</a></p>'
            )
            return self._html(body, title=f"Версия {version}", session=session)
        return self._error(404, "Страница не найдена.")

    def _html(self, body: str, *, title: str, session: sessions.Session, notice: str = "", status: int = 200) -> Response:
        page = render.layout(
            title=title,
            active=_active("/publish" if title.startswith("Публика") else "/"),
            body=body,
            actor=self._actor_name(session),
            csrf=session.csrf_token,
            notice=notice,
        )
        return Response(status, "text/html; charset=utf-8", page.encode("utf-8"), (("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS))

    # --- login ------------------------------------------------------------- #

    def _login(self, conn: sqlite3.Connection, environ: Mapping[str, Any]) -> Response:
        if self._session(conn, environ) is not None:
            return self._redirect(f"{ADMIN_ROOT}/")
        if self.oidc is None:
            widget = self._widget_form()
            return self._html_page(render.login_page(authorization_url=None, error="Провайдер OIDC не настроен.", widget=widget))
        try:
            url, pending = self.oidc.begin()
        except oidc.AuthError as exc:
            return self._html_page(render.login_page(authorization_url=None, error=str(exc)))
        headers = (
            ("Content-Type", "text/html; charset=utf-8"),
            *SECURITY_HEADERS,
            ("Set-Cookie", _pending_cookie(self.oidc.sign_pending(pending), secure=self.secure_cookies)),
        )
        return Response(303, "text/html; charset=utf-8", b"", (("Location", url), *headers))

    def _callback(self, conn: sqlite3.Connection, query: Mapping[str, str], environ: Mapping[str, Any]) -> Response:
        if self.oidc is None:
            return self._error(404, "Провайдер OIDC не настроен.")
        cookies = sessions.parse_cookies(environ.get("HTTP_COOKIE"))
        try:
            pending = self.oidc.read_pending(cookies.get(oidc.PENDING_COOKIE))
            identity = self.oidc.complete(code=str(query.get("code") or ""), state=query.get("state"), pending=pending)
        except oidc.AuthError as exc:
            sessions.record_login(conn, subject=None, outcome="denied", reason=str(exc), now=self.now())
            return self._html_page(render.login_page(authorization_url=None, error=str(exc)))
        next_path = pending.next_path
        return self._grant(conn, identity, next_path)

    def _widget_form(self) -> str:
        return (
            '<h2>Проверка подписи виджета</h2>'
            '<p>Классический Telegram Login принимается только с серверной проверкой подписи. '
            "Поля подписанного ответа вставляются оператором.</p>"
            '<form method="post" action="/admin/login/widget">'
            '<label for="wid">id</label><input id="wid" name="id">'
            '<label for="wname">first_name</label><input id="wname" name="first_name">'
            '<label for="wuser">username</label><input id="wuser" name="username">'
            '<label for="wdate">auth_date</label><input id="wdate" name="auth_date">'
            '<label for="whash">hash</label><input id="whash" name="hash">'
            "<button type=\"submit\">Войти</button></form>"
        )

    def _grant(self, conn: sqlite3.Connection, identity: oidc.Identity, next_path: str) -> Response:
        subject = identity.user_id
        if sessions.rate_limited(conn, subject, now=self.now(), limit=self.rate_limit_attempts):
            sessions.record_login(conn, subject=subject, outcome="rate_limited", reason="too many failures", now=self.now())
            return self._html_page(render.denied_page("Слишком много неудачных попыток входа. Повторите позже."), status=429)
        if not members.is_member(conn, subject):
            sessions.record_login(conn, subject=subject, outcome="denied", reason="not an active member", now=self.now())
            repo.audit(conn, action="admin.login.denied", actor=subject, entity_type="admin_member", entity_key=subject, detail={"method": identity.method}, now=self.now().isoformat())
            return self._html_page(
                render.denied_page("Подтверждённый Telegram-аккаунт не входит в список действующих коммунаров."),
                status=403,
            )
        session = sessions.create_session(conn, user_id=subject, display_name=identity.display_name or None, now=self.now())
        sessions.record_login(conn, subject=subject, outcome="ok", reason=identity.method, now=self.now())
        repo.audit(conn, action="admin.login", actor=subject, entity_type="admin_session", entity_key="session", detail={"method": identity.method}, now=self.now().isoformat())
        headers = (
            ("Content-Type", "text/html; charset=utf-8"),
            *SECURITY_HEADERS,
            sessions.cookie(session, secure=self.secure_cookies),
            ("Set-Cookie", _clear_pending_cookie(secure=self.secure_cookies)),
        )
        target = next_path if next_path.startswith(ADMIN_ROOT) else f"{ADMIN_ROOT}/"
        return Response(303, "text/html; charset=utf-8", b"", (("Location", target), *headers))

    def _logout(self, conn: sqlite3.Connection, form: Mapping[str, str], session: sessions.Session) -> Response:
        sessions.revoke(conn, session.session_id, reason="logout", now=self.now())
        repo.audit(conn, action="admin.logout", actor=session.user_id, entity_type="admin_session", entity_key="session", now=self.now().isoformat())
        headers = (("Location", f"{ADMIN_ROOT}/login"), *SECURITY_HEADERS, sessions.cleared_cookie(secure=self.secure_cookies))
        return Response(303, "text/html; charset=utf-8", b"", headers)

    # --- POST -------------------------------------------------------------- #

    def _post(self, conn: sqlite3.Connection, tail: str, environ: Mapping[str, Any]) -> Response:
        if tail == "/login/widget":
            return self._widget_login(conn, environ)
        session = self._session(conn, environ)
        if session is None:
            return self._error(403, "Действие требует активной сессии.", public=False)
        form = self._form(environ)
        if form is None:
            return self._error(413, "Тело запроса слишком велико или имеет неверный тип.")
        if not self._origin_ok(environ):
            return self._error(403, "Запрос пришёл с чужого источника.")
        if tail == "/logout":
            if not sessions.csrf_matches(session, form.get("csrf")):
                return self._error(403, "Токен CSRF не совпадает.")
            return self._logout(conn, form, session)
        if not tail.startswith("/actions/"):
            return self._error(404, "Действие не найдено.")
        if not sessions.csrf_matches(session, form.get("csrf")):
            repo.audit(conn, action="admin.csrf.rejected", actor=session.user_id, entity_type="action", entity_key=tail, now=self.now().isoformat())
            return self._error(403, "Токен CSRF не совпадает.")
        action = tail[len("/actions/"):].strip("/")
        try:
            message = self._perform(conn, action, form, session)
        except (desk.DeskError, news.NewsError, members.MemberError, reports.PublicationError, history_admin.HistoryError, ValueError) as exc:
            return self._action_result(conn, session, location=self._action_return(action, form), message=str(exc), status=409)
        return self._action_result(conn, session, location=self._action_return(action, form), message=message)

    def _action_result(self, conn: sqlite3.Connection, session: sessions.Session, *, location: str, message: str, status: int = 303) -> Response:
        if status == 303:
            return self._redirect(f"{location}?notice={_quote(message)}")
        body = (
            f'<h1>Действие отклонено</h1><p>{render.e(message)}</p>'
            f'<p><a href="{render.e(location)}">Вернуться</a></p>'
        )
        return self._html(body, title="Действие отклонено", session=session, status=status)

    def _perform(self, conn: sqlite3.Connection, action: str, form: Mapping[str, str], session: sessions.Session) -> str:
        actor = session.user_id
        now = self.now()
        if action == "verify":
            entity_type = str(form.get("entity_type") or "").strip()
            entity_key = str(form.get("entity_key") or "").strip()
            if entity_type not in checks.ENTITY_TYPES:
                raise ValueError("неизвестный тип сущности")
            if not entity_key:
                raise ValueError("нужен ключ сущности")
            result = checks.verify(conn, entity_type=entity_type, entity_key=entity_key, actor=actor, now=now.isoformat())
            return f"Отметка проверки: {render.STATE_LABELS.get(result.state, result.state)}"
        if action == "issue/ack":
            issue_id = str(form.get("issue_id") or "")
            comment = str(form.get("comment") or "")
            if not diagnostics.acknowledge(conn, issue_id, actor=actor, comment=comment, now=now.isoformat()):
                raise desk.DeskError("проблема не найдена или уже решена")
            return "Проблема отмечена известной; расхождение остаётся открытым"
        if action == "money/annotate":
            desk.annotate(conn, transaction_id=str(form.get("transaction_id") or ""), note=str(form.get("note") or ""), actor=actor, now=now.isoformat())
            return "Аннотация сохранена"
        if action == "money/edit":
            desk.edit_money(transaction_id=str(form.get("transaction_id") or ""), amount=str(form.get("amount") or ""))
        if action == "report/exclude":
            result = desk.exclude_operation(
                conn,
                transaction_id=str(form.get("transaction_id") or ""),
                reason=str(form.get("reason") or ""),
                actor=actor,
                now=now.isoformat(),
            )
            note = " (повторно включено ограничение)" if result.get("reactivated") else ""
            return "Операция исключена из отчёта обратимо" + note
        if action == "report/restore":
            desk.restore_operation(
                conn, transaction_id=str(form.get("transaction_id") or ""), actor=actor, now=now.isoformat()
            )
            return "Операция возвращена в отчёт"
        if action == "allocation/import":
            raw = str(form.get("allocation_json") or "").strip()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError as exc:
                raise desk.DeskError("политика распределения должна быть корректным JSON") from exc
            if not isinstance(payload, Mapping):
                raise desk.DeskError("политика распределения должна быть объектом JSON")
            version = desk.set_allocation_policy(conn, payload=payload, actor=actor, now=now.isoformat())
            return f"Правила распределения записаны новой версией {version}"
        if action == "category/hint":
            version = desk.set_category_hint(
                conn,
                category_id=str(form.get("category_id") or ""),
                hint=(str(form.get("hint")) if form.get("hint") else None),
                actor=actor,
                now=now.isoformat(),
            )
            return f"Подсказка категории записана новой версией {version}"
        if action == "mapping/category":
            version = desk.set_category_mapping(
                conn,
                category_id=str(form.get("category_id") or ""),
                economic_class=(str(form.get("economic_class")) if form.get("economic_class") else None),
                label=(str(form.get("label")) if form.get("label") else None),
                actor=actor,
                now=now.isoformat(),
            )
            return f"Сопоставление категории записано новой версией {version}"
        if action == "mapping/event":
            private_raw = str(form.get("private") or "").strip().lower()
            version = desk.set_event_mapping(
                conn,
                subcalendar_id=str(form.get("subcalendar_id") or ""),
                event_type=str(form.get("event_type") or "unknown"),
                private=private_raw in ("1", "true", "да", "yes", "on"),
                actor=actor,
                now=now.isoformat(),
                excluded=_checked(form.get("excluded")),
                meeting_contour=_checked(form.get("meeting_contour")),
                rent=_checked(form.get("rent")),
            )
            return f"Сопоставление события записано новой версией {version}"
        if action == "mapping/rollback":
            version = desk.rollback_mapping(conn, kind=str(form.get("kind") or ""), version=str(form.get("version") or ""), actor=actor, now=now.isoformat())
            return f"Сопоставление восстановлено новой версией {version}"
        if action == "news/edit":
            result = news.edit(
                conn,
                news_id=str(form.get("news_id") or ""),
                actor=actor,
                title=(str(form.get("title")) if form.get("title") else None),
                summary=(str(form.get("summary")) if form.get("summary") else None),
                category=(str(form["category"]) if "category" in form else None),
                now=now.isoformat(),
            )
            return f"Исправление записано ручной версией v{result['revision']}"
        if action == "news/hide":
            news.hide(conn, news_id=str(form.get("news_id") or ""), actor=actor, reason=str(form.get("reason") or ""), now=now.isoformat())
            return "Новость скрыта"
        if action == "news/restore":
            version_raw = str(form.get("version") or "").strip()
            result = news.restore(
                conn,
                news_id=str(form.get("news_id") or ""),
                actor=actor,
                version=int(version_raw) if version_raw.isdigit() else None,
                now=now.isoformat(),
            )
            return f"Новость восстановлена ({result['manual_state']})"
        if action == "news/merge":
            result = news.merge(
                conn,
                target_id=str(form.get("target_id") or ""),
                source_id=str(form.get("source_id") or ""),
                actor=actor,
                reason=str(form.get("reason") or ""),
                now=now.isoformat(),
            )
            return f"Новости объединены по исходным сообщениям ({len(result['source_message_ids'])})"
        if action == "publish/save":
            label = str(form.get("period") or "")
            status = str(form.get("status") or "").strip() or None
            result = reports.save_draft(
                conn,
                label=label,
                actor=actor,
                comment=(str(form.get("comment")) if "comment" in form else None),
                status=status,
                now=now.isoformat(),
            )
            return f"Черновик сохранён (статус {result['status']})"
        if action == "publish/close":
            result = reports.close_period(
                conn,
                label=str(form.get("period") or ""),
                actor=actor,
                reason=str(form.get("reason") or ""),
                now=now.isoformat(),
            )
            return f"Период закрыт, зафиксирована версия v{result['version']}"
        if action == "publish/correct":
            field_name = str(form.get("field") or "")
            value_raw = str(form.get("value") or "").strip()
            value = None if value_raw == "" else int(value_raw)
            result = reports.correction(
                conn,
                label=str(form.get("period") or ""),
                changes={field_name: value},
                reason=str(form.get("reason") or ""),
                actor=actor,
                now=now.isoformat(),
            )
            return f"Исправление записано версией v{result['version']}"
        if action == "members/add":
            member = members.add_member(conn, str(form.get("telegram_user_id") or ""), actor=actor, now=now.isoformat())
            return f"Коммунар {member.telegram_user_id} добавлен"
        if action == "members/remove":
            revoked = members.revoke_member(conn, str(form.get("telegram_user_id") or ""), actor=actor, now=now.isoformat())
            return f"Доступ отозван, сессий закрыто: {revoked}"
        if action == "news-source":
            result = desk.set_news_source(
                conn,
                source_id=str(form.get("source_id") or ""),
                channel_id=str(form.get("channel_id") or ""),
                enabled=str(form.get("enabled") or "").strip().lower() in ("1", "true", "да", "yes", "on"),
                actor=actor,
                now=now.isoformat(),
            )
            return f"Источник новостей {result['source_id']} обновлён"
        if action == "donor-alias":
            alias = desk.set_donor_alias(
                conn,
                alias_id=str(form.get("alias_id") or ""),
                public_label=str(form.get("public_label") or ""),
                allowed=str(form.get("allowed") or "").strip().lower() in ("1", "true", "да", "yes", "on"),
                actor=actor,
                note=(str(form.get("note")) if form.get("note") else None),
                now=now.isoformat(),
            )
            return f"Псевдоним донатера {alias.alias_id} сохранён"
        raise desk.DeskError("неизвестное действие")

    def _action_return(self, action: str, form: Mapping[str, str]) -> str:
        if action.startswith("news"):
            return f"{ADMIN_ROOT}/news"
        if action.startswith("report/"):
            transaction_id = str(form.get("transaction_id") or "")
            return f"{ADMIN_ROOT}/money/{transaction_id}" if transaction_id else f"{ADMIN_ROOT}/money"
        if action.startswith("mapping") or action.startswith("members") or action.startswith("allocation") or action.startswith("category/") or action in ("news-source", "donor-alias"):
            return f"{ADMIN_ROOT}/settings"
        if action.startswith("publish"):
            label = str(form.get("period") or "")
            return f"{ADMIN_ROOT}/publish/{label}" if label else f"{ADMIN_ROOT}/publish"
        if action.startswith("money"):
            return f"{ADMIN_ROOT}/money"
        if action == "verify" or action.startswith("issue"):
            return f"{ADMIN_ROOT}/reconciliation"
        return f"{ADMIN_ROOT}/"

    def _widget_login(self, conn: sqlite3.Connection, environ: Mapping[str, Any]) -> Response:
        if self.widget_bot_token is None:
            return self._error(404, "Подпись виджета не настроена.")
        form = self._form(environ)
        if form is None:
            return self._error(413, "Тело запроса слишком велико или имеет неверный тип.")
        try:
            identity = oidc.verify_login_widget(dict(form), bot_token=self.widget_bot_token, now=self.now)
        except oidc.AuthError as exc:
            sessions.record_login(conn, subject=None, outcome="bad_signature", reason=str(exc), now=self.now())
            return self._html_page(render.login_page(authorization_url=None, error=str(exc), widget=self._widget_form()), status=401)
        return self._grant(conn, identity, f"{ADMIN_ROOT}/")

    # --- helpers ----------------------------------------------------------- #

    def _session(self, conn: sqlite3.Connection, environ: Mapping[str, Any]) -> sessions.Session | None:
        cookies = sessions.parse_cookies(environ.get("HTTP_COOKIE"))
        session = sessions.load_session(conn, cookies.get(sessions.COOKIE_NAME), now=self.now())
        if session is None:
            return None
        if not members.is_member(conn, session.user_id):
            # the allowlist is authoritative: a revoked member loses the session at once
            sessions.revoke(conn, session.session_id, reason="revoked", now=self.now())
            return None
        return session

    def _actor_name(self, session: sessions.Session) -> str:
        name = (session.display_name or "").strip()
        return f"{name} (ID {session.user_id})" if name else f"ID {session.user_id}"

    def _form(self, environ: Mapping[str, Any]) -> dict[str, str] | None:
        ctype = str(environ.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/x-www-form-urlencoded":
            return None
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return None
        if length > MAX_BODY:
            return None
        stream = environ.get("wsgi.input")
        if stream is None:
            return {}
        raw = stream.read(length) if length else b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return {key: value for key, value in _parse_query(text).items()}

    def _origin_ok(self, environ: Mapping[str, Any]) -> bool:
        origin = environ.get("HTTP_ORIGIN")
        if origin:
            host = str(environ.get("HTTP_HOST") or "")
            return origin in (f"https://{host}", f"http://{host}")
        referer = environ.get("HTTP_REFERER")
        if referer:
            host = str(environ.get("HTTP_HOST") or "")
            return referer.startswith(f"https://{host}/") or referer.startswith(f"http://{host}/")
        return True

    def _static(self, tail: str) -> Response:
        name = tail[len("/static/"):]
        if "/" in name or name in ("", ".", ".."):
            return self._error(404, "Файл не найден.")
        target = (self.static_dir / name).resolve()
        if not str(target).startswith(str(self.static_dir.resolve())) or not target.is_file():
            return self._error(404, "Файл не найден.")
        ctype = "text/css; charset=utf-8" if target.suffix == ".css" else "application/octet-stream"
        return Response(200, ctype, target.read_bytes(), SECURITY_HEADERS)

    def _redirect(self, location: str) -> Response:
        return Response(303, "text/plain; charset=utf-8", b"", (("Location", location), *SECURITY_HEADERS))

    def _json(self, payload: Mapping[str, Any], *, status: int = 200) -> Response:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return Response(status, "application/json; charset=utf-8", body, SECURITY_HEADERS)

    def _html_page(self, body: str, *, status: int = 200) -> Response:
        return Response(status, "text/html; charset=utf-8", body.encode("utf-8"), (("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS))

    def _error(self, status: int, message: str, *, public: bool = False) -> Response:
        return self._html_page(render.denied_page(message), status=status)


def _finding_view(finding: diagnostics.Finding) -> dict[str, Any]:
    return {
        "code": finding.code,
        "severity": finding.severity,
        "title": finding.title,
        "explanation": finding.explanation,
        "significance": finding.significance,
        "sources": finding.sources,
        "periods": finding.periods,
        "entity_type": finding.entity_type,
        "entity_key": finding.entity_key,
        "status": diagnostics.OPEN,
    }


def _period_index(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    labels: set[str] = set(repo.report_period_labels(conn))
    for row in conn.execute("SELECT period_label FROM post_drafts"):
        labels.add(str(row["period_label"]))
    for item in desk.unclosed_periods(conn):
        labels.add(str(item["period"]))
    out = []
    for label in sorted(labels):
        try:
            state = reports.publication_state(conn, label)
        except reports.PublicationError:
            # one unreadable label must not take the whole panel down; the period
            # is listed (so the anomaly is visible) without a frozen state
            out.append({"period": label, "status": "период не распознан", "frozen_version": None, "readiness": {"ready": False}})
            continue
        out.append(
            {
                "period": label,
                "status": state["status"],
                "frozen_version": state["frozen_version"],
                "readiness": state["readiness"],
            }
        )
    return out


def _active(tail: str) -> str:
    for href, _label in render.SECTIONS:
        if href == f"{ADMIN_ROOT}{tail}" or (tail.startswith(href[len(ADMIN_ROOT):] + "/") and href != f"{ADMIN_ROOT}/"):
            return href
    return f"{ADMIN_ROOT}/"


def _pending_cookie(value: str, *, secure: bool) -> str:
    attrs = "Path=/admin; HttpOnly; SameSite=Lax; Max-Age=900"
    if secure:
        attrs += "; Secure"
    return f"{oidc.PENDING_COOKIE}={value}; {attrs}"


def _clear_pending_cookie(*, secure: bool) -> str:
    attrs = "Path=/admin; HttpOnly; SameSite=Lax; Max-Age=0"
    if secure:
        attrs += "; Secure"
    return f"{oidc.PENDING_COOKIE}=; {attrs}"


def _checked(value: str | None) -> bool:
    """HTML checkbox value: present-and-truthy means checked, absent means unchecked."""

    return str(value or "").strip().lower() in {"1", "true", "да", "yes", "on"}


def _parse_query(query_string: str) -> dict[str, str]:
    from urllib.parse import unquote_plus

    out: dict[str, str] = {}
    for chunk in (query_string or "").split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        out[unquote_plus(key)] = unquote_plus(value)
    return out


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value[:200], safe="")


def build_desk_from_env(
    *,
    db_path: str | None = None,
    secret: str | None = None,
    release_dir: str | None = None,
    transport: Callable[..., tuple[int, bytes]] | None = None,
    now: Callable[[], datetime] | None = None,
    require_secret: bool = True,
) -> AdminDesk:
    """Build the desk from the documented environment variables.

    No secret is ever printed. Without a server secret the desk refuses to start
    rather than run with a guessable one.
    """

    db = db_path or os.environ.get("RANREPORT_DB")
    if not db:
        raise ValueError("RANREPORT_DB is required for the editorial desk")
    key = secret or os.environ.get("RANREPORT_ADMIN_SECRET")
    if not key:
        if require_secret:
            raise ValueError("RANREPORT_ADMIN_SECRET is required (server-side secret, never a token)")
        key = "staging-secret-not-for-production"
    issuer = os.environ.get("TELEGRAM_OIDC_ISSUER")
    client_id = os.environ.get("TELEGRAM_OIDC_CLIENT_ID")
    redirect_uri = os.environ.get("TELEGRAM_OIDC_REDIRECT_URI")
    client = None
    if issuer and client_id and redirect_uri:
        client = oidc.TelegramOidc(
            issuer=issuer,
            client_id=client_id,
            redirect_uri=redirect_uri,
            secret=key,
            transport=transport or oidc.transport_http,
            now=now or (lambda: datetime.now(timezone.utc)),
            allowed_redirect_uris=tuple(
                item for item in (os.environ.get("TELEGRAM_OIDC_REDIRECT_ALLOWLIST") or redirect_uri).split(",") if item
            ),
            allow_insecure_loopback=os.environ.get("RANREPORT_ALLOW_INSECURE_LOOPBACK") == "1",
        )
    return AdminDesk(
        db_path=db,
        secret=key,
        oidc=client,
        widget_bot_token=os.environ.get("TELEGRAM_AUTH_BOT_TOKEN"),
        release_dir=release_dir or os.environ.get("RANREPORT_RELEASE_DIR"),
        now=now,
        secure_cookies=os.environ.get("RANREPORT_INSECURE_COOKIES") != "1",
    )
