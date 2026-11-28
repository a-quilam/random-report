"""The public site: SSR pages, the read-only API and the CSV export.

The application is deliberately a plain WSGI callable with no third-party
dependencies, so the whole public surface can be started with the standard
library and audited in one file. It serves two independent data planes:

* the **historical plane** (``randreport.history`` + report snapshots) — the
  transcribed public reports, always available;
* the **live plane** (``randreport.web.release_index``) — the allowlist release
  the nightly builder publishes, empty until real sources are connected.

Pages render only strict DTOs and historical observations. Query strings are
validated against the projection allowlist before any data is touched, so a
request for a single day, account or sensitive slice is refused with a generic
error that carries no internal identifier (sections 6.6, 10; R02).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .. import history
from ..domain.dto import ChronicleDTO, CommunityDTO, EventsDTO, MoneyDTO
from ..domain.timeframes import ALL_TIME, MONTH, YEAR, Period, msk_day, parse_period, range_period
from ..projection.csv_export import export_categories_csv
from ..projection.privacy import PrivacyLeakError, assert_no_leaks, public_json
from ..projection.query import (
    ALLOWED_QUERY_PARAMS,
    UnsafeQueryError,
    check_period_allowed,
    resolve_period,
    validate_params,
)
from . import render
from .release_index import ReleaseIndex, overview as release_overview
from . import reports as reports_module

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", CSP),
)

STATUS_TEXT = {200: "200 OK", 304: "304 Not Modified", 400: "400 Bad Request", 404: "404 Not Found", 405: "405 Method Not Allowed", 500: "500 Internal Server Error"}

REPORT_PATH_RE = re.compile(
    r"^/reports/(?P<label>[0-9]{4}(?:-[0-9]{2}(?:\.\.[0-9]{4}-[0-9]{2})?)?)(?:/v(?P<version>[0-9]+))?$"
)

MONEY_PERIOD_DEFAULT = "all"


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


class PublicSite:
    """WSGI application for the public site."""

    def __init__(
        self,
        *,
        release_dir: str | Path | None = None,
        db_path: str | Path | None = None,
        static_dir: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.index = ReleaseIndex(release_dir)
        self._db_path = Path(db_path) if db_path else None
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).with_name("static")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._conn: sqlite3.Connection | None = None

    # --- infrastructure --------------------------------------------------- #

    def connection(self) -> sqlite3.Connection | None:
        """A read-only connection to the report-snapshot store, if configured."""

        if self._db_path is None:
            return None
        if self._conn is None:
            if not self._db_path.exists():
                return None
            uri = f"file:{self._db_path}?mode=ro&immutable=0"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def today(self):
        return msk_day(self._now())

    def default_period(self) -> str:
        day = self.today()
        return f"{day.year:04d}-{day.month:02d}"

    def available_periods(self) -> tuple[str, ...]:
        months = sorted(set(self.index.periods()) | set(history.month_labels()) | {self.default_period()})
        years = sorted(set(str(y) for y in history.years()) | {p for p in self.index.periods() if len(p) == 4})
        return tuple(dict.fromkeys([*months, *years, "all"]))

    # --- data assembly ---------------------------------------------------- #

    def historical_views(self, period: Period) -> dict[str, Any]:
        return {
            "events": history.metric_view(period, "events"),
            "receipts": history.metric_view(period, "receipts"),
            "expenses": history.metric_view(period, "expenses"),
        }

    def release_for(self, period: Period) -> Any:
        return self.index.by_period(period.label)

    def comparisons(self, period: Period) -> tuple[tuple[str, str], ...]:
        """Previous month / same month last year / year-to-year, only where figures exist.

        An incomplete current period is never compared silently: the row is
        marked "по состоянию на" and the comparable elapsed period is left out
        unless a figure actually exists (section 3).
        """

        rows: list[tuple[str, str]] = []
        if period.kind == MONTH and period.start is not None:
            label = f"{period.start.year:04d}-{period.start.month:02d}"
            previous = _shift_month(label, -1)
            same_prev_year = _shift_month(label, -12)
            rows.append(self._compare_row("Предыдущий месяц", label, previous))
            rows.append(self._compare_row("Тот же месяц год назад", label, same_prev_year))
        elif period.kind == YEAR and period.start is not None:
            year = period.start.year
            rows.append(self._compare_row("Предыдущий год", str(year), str(year - 1), kind=YEAR))
        if not rows:
            return ()
        return tuple(rows)

    def _compare_row(self, caption: str, current_label: str, other_label: str, *, kind: str = MONTH) -> tuple[str, str]:
        current = self._events_value(current_label, kind)
        other = self._events_value(other_label, kind)
        if current is None or other is None:
            return (caption, f"нет данных ({other_label})")
        delta = current - other
        sign = "+" if delta > 0 else ""
        return (caption, f"{current} против {other} ({sign}{delta})")

    def _events_value(self, label: str, kind: str) -> int | None:
        try:
            period = parse_period(label)
        except Exception:  # noqa: BLE001 - an unparseable comparison label is simply unknown
            return None
        view = history.metric_view(period, "events")
        return view.value

    # --- WSGI ------------------------------------------------------------- #

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        try:
            response = self._dispatch(environ)
        except PrivacyLeakError:
            response = self._error(500, "Внутренняя проверка приватности остановила выпуск. Раздел временно скрыт.")
        except Exception:  # noqa: BLE001 - never leak an internal traceback to the public
            response = self._error(500, "Внутренняя ошибка. Попробуйте позже.")
        headers = list(response.headers)
        if not any(name.lower() == "content-type" for name, _value in headers):
            headers.insert(0, ("Content-Type", response.content_type))
        start_response(STATUS_TEXT.get(response.status, f"{response.status}"), headers)
        return [response.body]

    def close(self) -> None:
        """Release the read-only report-store connection."""

        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _dispatch(self, environ: Mapping[str, Any]) -> Response:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/") or "/")
        if method not in ("GET", "HEAD"):
            return self._error(405, "Поддерживается только чтение (GET).")
        query = _parse_query(environ.get("QUERY_STRING", ""))
        if path.startswith("/static/"):
            return self._static(path)
        if path == "/robots.txt":
            return Response(
                200,
                "text/plain; charset=utf-8",
                b"User-agent: *\nAllow: /\nDisallow: /api/internal/\n",
                SECURITY_HEADERS,
            )
        if path == "/healthz":
            return Response(200, "application/json; charset=utf-8", b'{"status":"ok"}', SECURITY_HEADERS)
        if path.startswith("/api/public/export/categories.csv"):
            return self._csv(query)
        if path.startswith("/api/public/"):
            return self._api(path, query)
        return self._html(path, query)

    # --- helpers ---------------------------------------------------------- #

    def _error(self, status: int, message: str) -> Response:
        body = (
            "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{status}</title><link rel=\"stylesheet\" href=\"/static/site.css\"></head><body>"
            '<main id="content"><h1>Запрос отклонён</h1>'
            f"<p>{render.e(message)}</p><p><a href=\"/\">На главную</a></p></main></body></html>"
        )
        return Response(status, "text/html; charset=utf-8", body.encode("utf-8"), SECURITY_HEADERS)

    def _static(self, path: str) -> Response:
        name = path[len("/static/") :]
        if "/" in name or name in ("", ".", ".."):
            return self._error(404, "Файл не найден.")
        target = (self.static_dir / name).resolve()
        if not str(target).startswith(str(self.static_dir.resolve())) or not target.is_file():
            return self._error(404, "Файл не найден.")
        ctype = "text/css; charset=utf-8" if target.suffix == ".css" else "application/octet-stream"
        if target.suffix in (".jpg", ".jpeg"):
            # the paper textures of the collage are real image assets; without the
            # right type, `X-Content-Type-Options: nosniff` would refuse them
            ctype = "image/jpeg"
        elif target.suffix == ".png":
            ctype = "image/png"
        elif target.suffix == ".webp":
            ctype = "image/webp"
        elif target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        return Response(
            200,
            ctype,
            target.read_bytes(),
            (("Cache-Control", "public, max-age=300"), *SECURITY_HEADERS),
        )

    # --- HTML ------------------------------------------------------------- #

    def _html(self, path: str, query: Mapping[str, str]) -> Response:
        match = REPORT_PATH_RE.match(path)
        if match is not None:
            return self._report_html(match)
        try:
            period = self._requested_period(query)
        except UnsafeQueryError as exc:
            return self._error(400, f"Недопустимый запрос: {exc}")

        period_label = period.label
        periods = self.available_periods()
        status = None

        if path == "/":
            return self._html_response(self._home(period), etag=self._etag_for(period))
        elif path == "/money":
            return self._html_response(self._money(period, periods))
        elif path == "/events":
            return self._html_response(self._events(period, periods), etag=self._etag_for(period))
        elif path == "/community":
            body, status = self._community(period)
            title = "Сообщество"
        elif path == "/support":
            body, status = self._support(period)
            title = "Поддержка"
        elif path == "/chronicle":
            body, status = self._chronicle(period)
            title = "Летопись"
        elif path == "/reports":
            body = render.reports_index(history.available_periods(), period=period_label)
            title = "Архив отчётов"
        elif path == "/methodology":
            body = render.methodology_section(period=period_label)
            title = "Методология"
        else:
            return self._error(404, "Страница не найдена.")

        page = render.layout(
            title=title,
            active=path if path in {href for href, _ in render.NAV} else "/reports",
            period=period_label,
            periods=periods,
            body=body,
            status=status or self._status_for(period),
            mast_head=render.home_mast_head(period_label) if path == "/" else "",
        )
        return self._html_response(page, etag=self._etag_for(period))

    def _requested_period(self, query: Mapping[str, str]) -> Period:
        period = resolve_period(_public_params(query, self.default_period()))
        check_period_allowed(period)
        return period

    def _status_for(self, period: Period) -> str:
        release = self.release_for(period)
        if release is not None and release.money is not None:
            money = release.money
            return render.status_line(
                period_label=period.label,
                as_of=money.as_of.isoformat(),
                freshness=money.freshness,
                verification=money.verification,
                completeness=money.completeness,
                source_labels=money.source_labels,
                release_id=release.release_id,
            )
        return render.status_line(
            period_label=period.label,
            as_of=None,
            freshness="missing",
            verification="unspecified",
            completeness="unknown",
            source_labels=("исторический архив",),
        )

    def _photo_url(self) -> str | None:
        """Адрес разрешённого фото подвала, когда ассет выложен в статический контур.

        The approved photograph is deliberately not in git: the operator installs
        it next to the served static files (`RANREPORT_STATIC_DIR`, by default the
        package's `static`). Without the asset the poster keeps its honest frame —
        the mock's drawing (`photo-placeholder.png`) is never published.
        """

        for name in render.PHOTO_ASSETS:
            if (self.static_dir / name).is_file():
                return f"/static/{name}"
        return None

    def _brand_logo_url(self) -> str | None:
        """Адрес логотипа владельца, когда ассет выложен в статический контур.

        The owner asked for their logo in place of the asterisk by the title. The
        compact public file ships in the package ``static`` directory; if an
        operator points ``RANREPORT_STATIC_DIR`` elsewhere and the file is absent,
        the decorative mark stays so the masthead never shows a broken image.
        """

        return f"/static/{render.BRAND_LOGO}" if (self.static_dir / render.BRAND_LOGO).is_file() else None

    def _money_logo_url(self) -> str | None:
        """Адрес логотипа владельца именно для листа «Деньги».

        The approved v1.2 sheet mounts the owner's original artwork
        (``brand-real-logo.webp``); the home poster keeps its own approved mark.
        Without the asset the sheet falls back to the shipped brand logo.
        """

        preferred = "brand-real-logo.webp"
        if (self.static_dir / preferred).is_file():
            return f"/static/{preferred}"
        return self._brand_logo_url()

    def _home(self, period: Period) -> str:
        release = self.release_for(period)
        money = release.money if release else None
        events = release.events if release else None
        community = release.community if release else None
        from .approved_home import announcements_view, page
        cards, ranking_note, ranking_empty = announcements_view(
            release.announcements if release else None
        )
        return page(
            period_label=period.label,
            money=money,
            events=events,
            community=community,
            status=self._status_for(period) + render.period_switcher('/', self.available_periods(), period.label),
            historical=self.historical_views(period),
            events_previous=self._previous_month_events(period),
            subscribers_extra=self._subscriber_history(period),
            announcements=cards,
            ranking_note=ranking_note,
            ranking_empty=ranking_empty,
            photo_url=self._photo_url(),
            logo_url=self._brand_logo_url(),
        )

    def _previous_month_events(self, period: Period) -> int | None:
        """Состоявшиеся мероприятия прошлого календарного месяца — или ничего.

        The poster prints a small figure under the events count; it is read from
        the previous month's own public release (or the published historical
        aggregate) rather than inferred from the current period.
        """

        if period.kind != MONTH or period.start is None:
            return None
        label = _shift_month(f"{period.start.year:04d}-{period.start.month:02d}", -1)
        try:
            previous = parse_period(label)
        except Exception:  # noqa: BLE001 - an odd label simply has no neighbour
            return None
        release = self.release_for(previous)
        events = release.events if release else None
        if events is not None:
            value = events.metrics.get("total_events")
            if value is not None:
                return int(value)
        return history.metric_view(previous, "events").value

    def _subscriber_history(self, period: Period) -> tuple[Any, ...]:
        """Датированные точки подписчиков из соседних публичных выпусков.

        A month's release carries only the snapshots inside that month, while the
        poster needs a base at least 30 days old to state a real change; the two
        neighbouring months are read as well. All of them are the same allowlisted
        public DTO, so nothing private crosses the boundary.
        """

        if period.kind != MONTH or period.start is None:
            return ()
        points: list[Any] = []
        seen: set[str] = set()
        for shift in (-1, -2):
            label = _shift_month(f"{period.start.year:04d}-{period.start.month:02d}", shift)
            try:
                previous = parse_period(label)
            except Exception:  # noqa: BLE001 - an odd label simply has no neighbour
                continue
            release = self.release_for(previous)
            community = release.community if release else None
            if community is None:
                continue
            for point in community.channel_points:
                key = point.on.isoformat()
                if key not in seen:
                    seen.add(key)
                    points.append(point)
        return tuple(points)

    def _money(self, period: Period, periods: Sequence[str]) -> str:
        from .money_owner import page
        from .money_owner_data import money_owner_data

        release = self.release_for(period)
        end = (period.end or self.today()).strftime('%Y-%m')
        labels = sorted(label for label in self.index.periods() if re.fullmatch(r'[0-9]{4}-[0-9]{2}', label) and label <= end)
        months = []
        for label in labels[-6:]:
            item = release if label == period.label else self.index.by_period(label)
            if item and item.money:
                months.append((label, money_owner_data(item.money)))
        return page(period=period, periods=periods, money=release.money if release else None, months=months,
                    historical=self.historical_views(period), status=self._status_for(period),
                    photo_url=self._photo_url(), logo_url=self._money_logo_url())

    def _events_photo_url(self) -> str | None:
        """Адрес фото подвала постера «Мероприятия», когда ассет выложен оператором.

        Тот же контур, что у фото главной: файла нет в git, страницу рисует
        честное состояние без фотографии, пока ассет не установлен.
        """

        for name in render.EVENTS_PHOTO_ASSETS:
            if (self.static_dir / name).is_file():
                return f"/static/{name}"
        return None

    def _events_logo_url(self) -> str | None:
        return f"/static/{render.EVENTS_LOGO}" if (self.static_dir / render.EVENTS_LOGO).is_file() else None

    def _events_month_labels(self, period: Period) -> tuple[str, ...]:
        """Месяцы с публичным выпуском событий — выбор периода постера."""

        labels = sorted(
            label for label in self.index.periods()
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}", label)
        )
        if period.label not in labels and len(period.label) == 7:
            labels.append(period.label)
            labels.sort()
        return tuple(labels)

    def _events(self, period: Period, periods: Sequence[str]) -> str:
        """Утверждённый постер «Мероприятия» на реальных публичных данных."""

        del periods  # выбор периода постера ограничен месяцами с выпуском
        from .events_owner import page
        release = self.release_for(period)
        events = release.events if release else None
        prev_total = self._previous_month_events(period)
        prev_month = self._previous_month_number(period)
        return page(
            period_label=period.label,
            month_options=self._events_month_labels(period),
            events=events,
            prev_total=prev_total,
            prev_month=prev_month,
            photo_url=self._events_photo_url(),
            logo_url=self._events_logo_url(),
        )

    def _previous_month_number(self, period: Period) -> int | None:
        if period.kind != MONTH or period.start is None:
            return None
        previous = _shift_month(f"{period.start.year:04d}-{period.start.month:02d}", -1)
        return int(previous[5:7])

    def _community(self, period: Period) -> tuple[str, str | None]:
        release = self.release_for(period)
        return render.community_section(release.community if release else None, period_label=period.label), None

    def _support(self, period: Period) -> tuple[str, str | None]:
        release = self.release_for(period)
        return (
            render.support_section(release.money if release else None, period_label=period.label, historical=self.historical_views(period)),
            None,
        )

    def _chronicle(self, period: Period) -> tuple[str, str | None]:
        release = self.release_for(period)
        return render.chronicle_section(release.chronicle if release else None), None

    def _report_html(self, match: "re.Match[str]") -> Response:
        label = match.group("label")
        version = match.group("version")
        try:
            period = parse_report_label(label)
        except Exception:  # noqa: BLE001 - an impossible label is simply not found
            return self._error(404, "Отчёт не найден.")
        conn = self.connection()
        reports = history.reports_for_period(period)
        report = history.exact_report(period)
        covered_by = report if report is not None else history.covering_report(period)
        public: dict[str, Any] = {"latest": None, "changed": False, "corrections": (), "versions": ()}
        versions_rows: list[tuple[str, str, str, str]] = []
        if conn is not None:
            stored = reports_module.public_view(conn, label)
        else:
            # the public process has no access to the private database: the version
            # archive comes from the allowlist store the builder published
            entry = self.index.report_entry(label)
            stored = reports_module.view_from_entry(entry) if entry is not None else public
        if stored.get("versions"):
            public = stored
            for item in stored["versions"]:
                versions_rows.append(
                    (
                        f"v{item['version']}",
                        render.e(item["published_at"]),
                        render.e(render.PROVENANCE_LABELS.get(str(item["provenance"]), item["provenance"])),
                        f'<a href="{render.e(render.safe_url(item["url"]))}">постоянная ссылка</a>',
                    )
                )
        payload = public.get("latest")
        if payload is None and report is not None:
            # no stored snapshot yet: render the published observation itself
            payload = reports_module.snapshot_payload(report)
        if version is not None:
            if conn is not None:
                payload = reports_module.version_payload(conn, label, int(version))
            else:
                entry = self.index.report_entry(label)
                payload = reports_module.version_payload_from_entry(entry, int(version))
            if payload is None:
                return self._error(404, "Версия отчёта не найдена.")
            public = {**public, "latest": payload, "changed": False, "corrections": ()}
        elif payload is not None:
            public = {**public, "latest": payload}
        text = None
        if payload is not None:
            text = reports_module.report_text(payload)
            reports_module.assert_text_faithful(text, payload)
        active = "/reports"
        page = render.layout(
            title=f"Отчёт за {label}",
            active=active,
            period=label,
            periods=self.available_periods(),
            body=render.report_page(
                period_label=label,
                report=report,
                public=public,
                text=text,
                versions_rows=versions_rows,
                covered_by=covered_by,
            ),
            status=self._report_status(label, payload, covered_by),
        )
        return self._html_response(page, etag=f'"report-{label}-{version or "latest"}-{len(reports)}"')

    def _report_status(self, label: str, payload: Mapping[str, Any] | None, covered_by: Any) -> str:
        if payload is None:
            if covered_by is not None:
                return render.status_line(
                    period_label=label,
                    as_of=covered_by.published,
                    freshness="fresh",
                    verification="operational",
                    completeness="partial",
                    source_labels=("исторический архив",),
                )
            return render.status_line(
                period_label=label,
                as_of=None,
                freshness="missing",
                verification="unspecified",
                completeness="unknown",
                source_labels=("исторический архив",),
            )
        return render.status_line(
            period_label=label,
            as_of=str(payload.get("published_on") or "") or None,
            freshness="fresh",
            verification="operational",
            completeness=str(payload.get("coverage", "unknown")),
            source_labels=("исторический архив",),
        )

    # --- API -------------------------------------------------------------- #

    def _api(self, path: str, query: Mapping[str, str]) -> Response:
        handler = {
            "/api/public/overview": self._api_overview,
            "/api/public/money": self._api_money,
            "/api/public/events": self._api_events,
            "/api/public/community": self._api_community,
            "/api/public/chronicle": self._api_chronicle,
        }.get(path)
        if handler is None:
            return self._json_error(404, "unknown endpoint")
        try:
            period = self._requested_period(query)
        except UnsafeQueryError as exc:
            return self._json_error(400, f"unsupported request: {exc}")
        return handler(period)

    def _api_overview(self, period: Period) -> Response:
        release = self.release_for(period)
        if release is not None:
            payload = release_overview(release)
        else:
            payload = {
                "release_id": None,
                "period": period.label,
                "as_of": None,
                "freshness": "missing",
                "verification": "unspecified",
                "metrics": {},
                "categories": [],
                "events_metrics": {},
                "event_type_aggregates": {},
                "community_metrics": {},
                "source_labels": ["исторический архив"],
            }
        payload["historical"] = {
            name: {
                "value": view.value,
                "unit": view.unit,
                "coverage": view.coverage,
                "period": period.label,
            }
            for name, view in self.historical_views(period).items()
        }
        return self._json(payload, etag=self._etag_for(period))

    def _release_section_response(self, period: Period, name: str) -> Response:
        release = self.release_for(period)
        if release is None or name not in release.sections:
            return self._json(
                {
                    "period": period.label,
                    "as_of": None,
                    "freshness": "missing",
                    "completeness": "unknown",
                    "message": "запрошенный период не опубликован",
                },
                status=404,
            )
        return self._json(release.sections[name].to_public_dict(), etag=f'"{release.release_id}-{name}"')

    def _api_money(self, period: Period) -> Response:
        return self._release_section_response(period, "money")

    def _api_events(self, period: Period) -> Response:
        return self._release_section_response(period, "events")

    def _api_community(self, period: Period) -> Response:
        return self._release_section_response(period, "community")

    def _api_chronicle(self, period: Period) -> Response:
        return self._release_section_response(period, "chronicle")

    # --- CSV -------------------------------------------------------------- #

    def _csv(self, query: Mapping[str, str]) -> Response:
        try:
            period = self._requested_period(query)
        except UnsafeQueryError as exc:
            return self._error(400, f"Недопустимый запрос: {exc}")
        if period.kind not in (MONTH, YEAR):
            return self._error(400, "CSV доступен только за целый месяц или целый год.")
        release = self.release_for(period)
        money = release.money if release else None
        if money is None:
            return self._error(404, "За этот период публичный выпуск ещё не сформирован, поэтому CSV недоступен.")
        text = export_categories_csv(money, params={"period": period.label, "unit": money.unit})
        return Response(
            200,
            "text/csv; charset=utf-8",
            text.encode("utf-8"),
            (
                ("Content-Disposition", f'attachment; filename="categories-{period.label}.csv"'),
                ("Cache-Control", "public, max-age=300"),
                *SECURITY_HEADERS,
            ),
        )

    # --- responses -------------------------------------------------------- #

    def _html_response(self, page: str, *, etag: str | None = None) -> Response:
        body = page.encode("utf-8")
        headers = (("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS)
        if etag:
            headers = (*headers, ("ETag", etag))
        return Response(200, "text/html; charset=utf-8", body, headers)

    def _json(self, payload: Mapping[str, Any], *, status: int = 200, etag: str | None = None, guarded: bool = True) -> Response:
        # ``guarded`` runs the privacy allowlist over the payload. A success
        # payload always goes through it; an *error* body only echoes the
        # caller's own request back, so guarding it would turn a legitimate 400
        # (for example a request carrying an id-looking parameter name) into a
        # 500 merely because the guard recognises the caller's text as private.
        if guarded:
            text = public_json(payload)
        else:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        headers = (("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "public, max-age=300"), *SECURITY_HEADERS)
        if etag:
            headers = (*headers, ("ETag", etag))
        return Response(status, "application/json; charset=utf-8", text.encode("utf-8"), headers)

    def _json_error(self, status: int, message: str) -> Response:
        return self._json({"error": message}, status=status, guarded=False)

    def _etag_for(self, period: Period) -> str:
        release = self.release_for(period)
        if release is not None:
            return f'"{release.release_id}"'
        return f'"historical-{period.label}-{len(history.published_reports())}"'

    # --- static export ---------------------------------------------------- #

    def export_static(self, out_dir: str | Path, *, periods: Sequence[str] | None = None) -> list[Path]:
        """Write every public page as a static file with a stable URL (section 3.9)."""

        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)
        wanted = tuple(periods) if periods else self.available_periods()
        written: list[Path] = []
        seen: set[str] = set()

        def write(rel: str, text: str) -> None:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            if rel not in seen:
                seen.add(rel)
                written.append(target)

        for period_label in wanted:
            for route, name in (
                ("/", "index.html"),
                ("/money", "money/index.html"),
                ("/events", "events/index.html"),
                ("/community", "community/index.html"),
                ("/support", "support/index.html"),
                ("/chronicle", "chronicle/index.html"),
                ("/reports", "reports/index.html"),
                ("/methodology", "methodology/index.html"),
            ):
                response = self._dispatch(
                    {"REQUEST_METHOD": "GET", "PATH_INFO": route, "QUERY_STRING": f"period={period_label}"}
                )
                rel = name if period_label == "all" else f"{period_label}/{name}"
                write(rel, response.text)
        for start, end, _grain in history.available_periods():
            for label in {start, end, start[:4], end[:4]}:
                response = self._dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": f"/reports/{label}", "QUERY_STRING": ""})
                write(f"reports/{label}/index.html", response.text)
        write("robots.txt", "User-agent: *\nAllow: /\n")
        return written


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _public_params(query: Mapping[str, str], default_period: str) -> dict[str, str]:
    """Reject anything outside the public allowlist, then validate the rest.

    A non-standard parameter is refused outright (not merely ignored): the
    projection allowlist is the only way a caller may shape a public response
    (sections 6.6, 10; R02).
    """

    unknown = sorted(set(query) - set(ALLOWED_QUERY_PARAMS))
    if unknown:
        raise UnsafeQueryError(f"parameters outside the public allowlist: {unknown}")
    merged = {"period": query.get("period") or default_period}
    for key in ("granularity", "unit"):
        if key in query:
            merged[key] = query[key]
    return validate_params(merged)


def parse_report_label(label: str) -> Period:
    """``YYYY``, ``YYYY-MM`` or a whole-month range ``YYYY-MM..YYYY-MM``."""

    if ".." in label:
        start, end = label.split("..", 1)
        return range_period(start, end)
    return parse_period(label)


def _parse_query(query_string: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (query_string or "").split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        out[_unquote(key)] = _unquote(value)
    return out


def _unquote(value: str) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(value)


def _shift_month(label: str, months: int) -> str:
    year, month = (int(part) for part in label.split("-"))
    index = year * 12 + (month - 1) + months
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"
