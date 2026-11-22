"""Zenmoney read-only diff client (docs/REQUIREMENTS.md section 6).

The client sends only synchronisation markers. The request body is validated
against an explicit allowlist, so a change array (transactions, accounts, tags,
instruments, deletions, budget) can never leave the process: this is the
technical read-only guarantee required when the API has no read-only scope.

The exact endpoint schema is a moving target; it is verified against the official
documentation at integration time (section 6.1), which is why the normaliser is
defensive and refuses unknown shapes instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..domain.finance import Movement
from ..domain.timeframes import MSK, msk_day
from .transport import HttpStatusError, Request, Response, Transport, TransportError

DIFF_PATH = "/v8/diff/"
DEFAULT_BASE_URL = "https://api.zenmoney.ru"

# The only body keys a read-only client may send: the two sync timestamps.
READ_REQUEST_KEYS = frozenset({"currentClientTimestamp", "serverTimestamp"})

# Keys that carry changes and must never be sent by this client.
FORBIDDEN_REQUEST_KEYS = frozenset(
    {"transaction", "transactions", "account", "accounts", "tag", "tags", "instrument",
     "instruments", "deletion", "deletions", "budget", "reminder", "user", "merchant"}
)

ZENMONEY_SOURCE = "zenmoney"


class ReadOnlyViolation(RuntimeError):
    """A request or payload that would write to the source."""


class ZenmoneyProtocolError(RuntimeError):
    """A response that does not match the documented read schema."""


@dataclass(frozen=True)
class ZenmoneyRow:
    """One normalised money movement as read from the diff payload."""

    transaction_id: str
    occurred_on: date
    amount_kopecks: int
    source_account: str | None = None
    target_account: str | None = None
    category_id: str | None = None
    currency: str = "RUB"
    deleted: bool = False
    revision: int = 1


@dataclass(frozen=True)
class ZenmoneyDiff:
    server_timestamp: int
    movements: tuple[Movement, ...]
    deleted_ids: tuple[str, ...]
    rows: tuple[ZenmoneyRow, ...]
    skipped_zero_amount: int = 0
    currency_unknown: tuple[str, ...] = ()
    instruments_seen: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)


def _parse_moment(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ZenmoneyProtocolError(f"unexpected transaction date {value!r}")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        # Zenmoney timestamps are local Moscow instants; the fact date is MSK
        moment = moment.replace(tzinfo=MSK)
    return moment


class ZenmoneyClient:
    """Read-only Zenmoney diff client."""

    source = ZENMONEY_SOURCE

    def __init__(
        self,
        transport: Transport,
        *,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        instrument_currencies: Mapping[int, str] | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._transport = transport
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._instrument_currencies = dict(instrument_currencies or {})
        self._timeout_s = timeout_s

    def __repr__(self) -> str:  # never leak the credential
        return f"ZenmoneyClient(base_url={self._base_url!r}, token={'set' if self._token else None})"

    # --- read-only gate ---------------------------------------------------- #

    @staticmethod
    def assert_read_request(payload: Mapping[str, Any]) -> None:
        """Reject any request body that is not purely a read/diff request."""

        forbidden = sorted(set(payload) & FORBIDDEN_REQUEST_KEYS)
        if forbidden:
            raise ReadOnlyViolation(
                f"read-only client refuses to send change arrays: {forbidden}"
            )
        unknown = sorted(set(payload) - READ_REQUEST_KEYS)
        if unknown:
            raise ReadOnlyViolation(f"request keys outside the read allowlist: {unknown}")

    # --- reading ----------------------------------------------------------- #

    def fetch_diff(
        self,
        *,
        server_timestamp: int = 0,
        current_client_timestamp: int | None = None,
        now: datetime | None = None,
    ) -> ZenmoneyDiff:
        """Fetch the initial full diff or an incremental diff by ``serverTimestamp``."""

        moment = now or datetime.now(timezone.utc)
        body = {
            "currentClientTimestamp": int(current_client_timestamp or moment.timestamp()),
            "serverTimestamp": int(server_timestamp),
        }
        self.assert_read_request(body)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(
            method="POST",
            url=f"{self._base_url}{DIFF_PATH}",
            json_body=body,
            headers=headers,
            timeout_s=self._timeout_s,
        )
        response = self._send(request)
        if not response.ok:
            raise HttpStatusError(response.status, response.text())
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise ZenmoneyProtocolError("diff response is not JSON") from exc
        return self.normalize(payload, revision_hint=server_timestamp)

    def _send(self, request: Request) -> Response:
        try:
            return self._transport.send(request)
        except HttpStatusError:
            raise
        except TransportError as exc:  # pragma: no cover - network path
            raise TransportError(str(exc)) from exc

    # --- normalisation ----------------------------------------------------- #

    def normalize(self, payload: Mapping[str, Any], *, revision_hint: int = 0) -> ZenmoneyDiff:
        """Turn a diff response into domain movements; never into an outgoing change."""

        if "serverTimestamp" not in payload:
            raise ZenmoneyProtocolError("diff response without serverTimestamp")
        server_timestamp = int(payload["serverTimestamp"])
        transactions: Sequence[Mapping[str, Any]] = payload.get("transaction") or ()
        if not isinstance(transactions, (list, tuple)):
            raise ZenmoneyProtocolError("transaction must be a list")

        rows: list[ZenmoneyRow] = []
        deleted_ids: list[str] = []
        skipped_zero = 0
        currency_unknown: list[str] = []
        instruments_seen: set[str] = set()

        for raw in transactions:
            if not isinstance(raw, Mapping) or "id" not in raw:
                raise ZenmoneyProtocolError("transaction without an id")
            transaction_id = str(raw["id"])
            if raw.get("deleted"):
                deleted_ids.append(transaction_id)
                continue
            currency = self._currency(raw, currency_unknown, instruments_seen)
            income = int(raw.get("income") or 0)
            outcome = int(raw.get("outcome") or 0)
            amount = income or outcome
            if amount == 0:
                # a zero move is not a money movement; never fabricate a kopeck
                skipped_zero += 1
                continue
            tags = raw.get("tag") or []
            if tags and not isinstance(tags, (list, tuple)):
                raise ZenmoneyProtocolError("tag must be a list")
            rows.append(
                ZenmoneyRow(
                    transaction_id=transaction_id,
                    occurred_on=msk_day(_parse_moment(raw.get("date"))),
                    amount_kopecks=amount,
                    source_account=raw.get("outcomeAccount"),
                    target_account=raw.get("incomeAccount"),
                    category_id=str(tags[0]) if tags else None,
                    currency=currency,
                    deleted=False,
                    revision=int(raw.get("revision") or 1),
                )
            )

        movements = self._rows_to_movements(rows)
        counts = {"rows": len(rows), "deleted": len(deleted_ids), "movements": len(movements), "skipped_zero": skipped_zero}
        return ZenmoneyDiff(
            server_timestamp=server_timestamp,
            movements=movements,
            deleted_ids=tuple(deleted_ids),
            rows=tuple(rows),
            skipped_zero_amount=skipped_zero,
            currency_unknown=tuple(currency_unknown),
            instruments_seen=tuple(sorted(instruments_seen)),
            counts=counts,
        )

    def _currency(self, raw: Mapping[str, Any], unknown: list[str], seen: set[str]) -> str:
        for key in ("incomeInstrument", "outcomeInstrument"):
            instrument = raw.get(key)
            if instrument is None:
                continue
            seen.add(str(instrument))
            currency = self._instrument_currencies.get(int(instrument))
            if currency is None:
                # an unknown instrument must not be silently treated as rubles;
                # "ZZZ" is an explicit sentinel that keeps the note on the row
                unknown.append(str(instrument))
                return "ZZZ"
            return currency
        return "RUB"

    @staticmethod
    def _rows_to_movements(rows: Iterable[ZenmoneyRow]) -> tuple[Movement, ...]:
        """Split a two-account row into the two legs the domain collapses.

        A Zenmoney transfer is one row with an outcome and an income account; the
        domain collapses the paired legs into a single ``internal_transfer`` and
        never double-counts them.
        """

        movements: list[Movement] = []
        for row in rows:
            notes: tuple[str, ...] = ("currency_unknown",) if row.currency == "ZZZ" else ()
            currency = "RUB" if row.currency == "ZZZ" else row.currency
            if row.source_account and row.target_account:
                pair_id = f"pair:{row.transaction_id}"
                movements.append(
                    Movement(
                        id=f"{row.transaction_id}:out",
                        occurred_on=row.occurred_on,
                        amount_kopecks=row.amount_kopecks,
                        source_account=row.source_account,
                        category_id=row.category_id,
                        currency=currency,
                        pair_id=pair_id,
                        pair_kind="transfer_leg",
                        source_revision=row.revision,
                        notes=notes,
                    )
                )
                movements.append(
                    Movement(
                        id=f"{row.transaction_id}:in",
                        occurred_on=row.occurred_on,
                        amount_kopecks=row.amount_kopecks,
                        target_account=row.target_account,
                        category_id=row.category_id,
                        currency=currency,
                        pair_id=pair_id,
                        pair_kind="transfer_leg",
                        source_revision=row.revision,
                        notes=notes,
                    )
                )
                continue
            movements.append(
                Movement(
                    id=row.transaction_id,
                    occurred_on=row.occurred_on,
                    amount_kopecks=row.amount_kopecks,
                    source_account=row.source_account,
                    target_account=row.target_account,
                    category_id=row.category_id,
                    currency=currency,
                    source_revision=row.revision,
                    notes=notes,
                )
            )
        return tuple(movements)
