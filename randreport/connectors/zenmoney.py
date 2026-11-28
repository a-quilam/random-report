"""Zenmoney read-only diff client (docs/REQUIREMENTS.md section 6).

The client sends only synchronisation markers. The request body is validated
against an explicit allowlist, so a change array (transactions, accounts, tags,
instruments, deletions, budget) can never leave the process: this is the
technical read-only guarantee required when the API has no read-only scope.

The normaliser follows the documented ``/v8/diff/`` shape (ZenPlugins wiki
"ZenMoney API", section 6.1): a transaction always names *both* an income and an
outcome account, and the **accounts** decide what the row means --

* the two accounts are the same one -> a single money move: an expense when
  ``outcome > 0``, an income (including a refund) when ``income > 0``;
* the two accounts differ -> a transfer, with ``outcome`` debited from the
  outcome account and ``income`` credited to the income account; the two wings
  therefore carry their own amount *and* their own instrument, so a
  cross-currency transfer is never collapsed into one number;
* a debt account on one side is an account of unknown scope for the public
  contour and is quarantined by the policy instead of being summed silently.

The exact endpoint schema is a moving target; it is verified against the official
documentation at integration time (section 6.1), which is why the normaliser is
defensive and refuses unknown shapes instead of guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from ..domain.finance import AccountBalance, Movement, ScopePolicy
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
UNKNOWN_CURRENCY = "ZZZ"

#: Account types whose money is immediately available (ZenMoney ``Account.type``).
#: ``deposit`` is real money but not liquid; ``loan``/``debt`` are liabilities the
#: public contour must never add to its cash.  Belonging to the contour (scope) is
#: a separate fact and never implies liquidity.
LIQUID_ACCOUNT_TYPES = frozenset({"cash", "ccard", "checking", "emoney"})
LIABILITY_ACCOUNT_TYPES = frozenset({"loan", "debt"})

#: Symbols/titles the API uses for a (system) instrument, mapped to ISO codes.
CURRENCY_CODES = {
    "rub": "RUB", "\u20bd": "RUB", "\u0440\u0443\u0431": "RUB", "\u0440\u0443\u0431\u043b\u044c": "RUB",
    "usd": "USD", "$": "USD", "\u0434\u043e\u043b\u043b\u0430\u0440": "USD",
    "eur": "EUR", "\u20ac": "EUR", "\u0435\u0432\u0440\u043e": "EUR",
    "kzt": "KZT", "\u20b8": "KZT", "byn": "BYN", "byr": "BYN",
    "uah": "UAH", "\u20b4": "UAH", "gel": "GEL", "\u20be": "GEL",
    "gbp": "GBP", "\u00a3": "GBP",
}


class ReadOnlyViolation(RuntimeError):
    """A request or payload that would write to the source."""


class ZenmoneyProtocolError(RuntimeError):
    """A response that does not match the documented read schema."""


@dataclass(frozen=True)
class ZenmoneyRow:
    """One normalised money row as read from the diff payload."""

    transaction_id: str
    occurred_on: date
    amount_kopecks: int
    source_account: str | None = None
    target_account: str | None = None
    category_id: str | None = None
    currency: str = "RUB"
    deleted: bool = False
    revision: int = 1
    #: the two sides of the row exactly as the source stated them
    income_kopecks: int = 0
    outcome_kopecks: int = 0
    kind: str = "money"  # money|transfer
    notes: tuple[str, ...] = ()
    held: bool = False


@dataclass(frozen=True)
class ZenmoneyAccount:
    """One account of the diff payload: its snapshot and its own nature."""

    account_id: str
    account_type: str | None = None
    currency: str = "RUB"
    balance_kopecks: int | None = None
    archived: bool = False
    title: str | None = None

    @property
    def has_snapshot(self) -> bool:
        return self.balance_kopecks is not None

    @property
    def liquid(self) -> bool:
        """Liquidity comes from the account itself, never from its scope."""

        return self.account_type in LIQUID_ACCOUNT_TYPES and not self.archived


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
    account_rows: tuple[ZenmoneyAccount, ...] = ()


def _parse_moment(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ZenmoneyProtocolError(f"unexpected transaction date {value!r}")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ZenmoneyProtocolError(f"unexpected transaction date {value!r}") from exc
    if moment.tzinfo is None:
        # Zenmoney timestamps are local Moscow instants; the fact date is MSK
        moment = moment.replace(tzinfo=MSK)
    return moment


def _account_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _amount_kopecks(value: Any, notes: list[str] | None = None) -> int:
    """Convert wire major units to signed SQLite minor units without rounding."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ZenmoneyProtocolError("invalid money amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ZenmoneyProtocolError("invalid money amount") from exc
    if not amount.is_finite() or amount.copy_abs() > Decimal("92233720368547758.07"):
        raise ZenmoneyProtocolError("amount outside supported range")
    sign, digits, exponent = amount.as_tuple()
    minor = Decimal((sign, digits, exponent + 2))
    if minor != minor.to_integral_value():
        raise ZenmoneyProtocolError("amount contains a fractional minor unit")
    return int(minor)


def currency_code(*values: Any) -> str | None:
    """Resolve an ISO code from the titles/symbols the API states for a currency."""

    for value in values:
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in CURRENCY_CODES:
            return CURRENCY_CODES[text]
        upper = text.upper()
        if len(upper) == 3 and upper.isascii() and upper.isalpha():
            return upper
    return None


def account_balances(
    accounts: Sequence[ZenmoneyAccount],
    *,
    policy: ScopePolicy,
    as_of: date,
) -> tuple[AccountBalance, ...]:
    """Turn account rows into dated snapshots with an explicit liquidity.

    Only a row that actually states a ``balance`` is a snapshot (a confirmed
    figure).  The account's *type* decides liquidity; its *belonging* to the
    public contour is recorded separately as the scope, because confirmation of
    membership is not liquidity.
    """

    out: list[AccountBalance] = []
    for account in accounts:
        balance = account.balance_kopecks
        if balance is None:
            # only a row that states a balance is a snapshot the contour can use
            continue
        out.append(
            AccountBalance(
                account_id=account.account_id,
                currency=account.currency,
                balance_kopecks=int(balance),
                as_of=as_of,
                is_liquid=account.liquid,
                confirmed=True,
                scope=policy.account_scope(account.account_id),
            )
        )
    return tuple(out)


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
        self._instrument_currencies = {int(key): str(value) for key, value in (instrument_currencies or {}).items()}
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
            payload = json.loads(response.body, parse_float=Decimal)
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

    def _instrument_map(self, payload: Mapping[str, Any]) -> dict[int, str]:
        """Currency of every instrument the payload states, plus the configured ones."""

        resolved: dict[int, str] = {}
        for raw in payload.get("instrument") or ():
            if not isinstance(raw, Mapping) or raw.get("id") is None:
                continue
            code = currency_code(raw.get("title"), raw.get("shortTitle"), raw.get("symbol"), raw.get("code"))
            if not code:
                continue
            try:
                resolved[int(raw["id"])] = code
            except (TypeError, ValueError):
                continue
        # a currency configured explicitly always wins over a derived one
        resolved.update(self._instrument_currencies)
        return resolved

    def _resolver(
        self,
        instruments: Mapping[int, str],
        unknown: list[str],
        seen: set[str],
    ) -> Callable[[Any], str]:
        def resolve(instrument: Any) -> str:
            if instrument is None:
                return "RUB"
            seen.add(str(instrument))
            try:
                key = int(instrument)
            except (TypeError, ValueError):
                unknown.append(str(instrument))
                return UNKNOWN_CURRENCY
            currency = instruments.get(key)
            if currency is None:
                # an unknown instrument must not be silently treated as rubles;
                # "ZZZ" is an explicit sentinel that keeps the note on the row
                unknown.append(str(instrument))
                return UNKNOWN_CURRENCY
            return currency

        return resolve

    def normalize(self, payload: Mapping[str, Any], *, revision_hint: int = 0) -> ZenmoneyDiff:
        """Turn a diff response into domain movements; never into an outgoing change."""

        if "serverTimestamp" not in payload:
            raise ZenmoneyProtocolError("diff response without serverTimestamp")
        server_timestamp = int(payload["serverTimestamp"])
        transactions: Sequence[Mapping[str, Any]] = payload.get("transaction") or ()
        if not isinstance(transactions, (list, tuple)):
            raise ZenmoneyProtocolError("transaction must be a list")

        instruments = self._instrument_map(payload)
        rows: list[ZenmoneyRow] = []
        movements: list[Movement] = []
        deleted_ids: list[str] = []
        skipped_zero = 0
        currency_unknown: list[str] = []
        instruments_seen: set[str] = set()
        resolve = self._resolver(instruments, currency_unknown, instruments_seen)

        for raw in transactions:
            if not isinstance(raw, Mapping) or "id" not in raw:
                raise ZenmoneyProtocolError("transaction without an id")
            transaction_id = str(raw["id"])
            if raw.get("deleted"):
                deleted_ids.append(transaction_id)
                continue
            notes: list[str] = []
            income = _amount_kopecks(raw.get("income"), notes)
            outcome = _amount_kopecks(raw.get("outcome"), notes)
            if income == 0 and outcome == 0:
                # a zero move is not a money movement; never fabricate a kopeck
                skipped_zero += 1
                continue
            tags = raw.get("tag") or []
            if tags and not isinstance(tags, (list, tuple)):
                raise ZenmoneyProtocolError("tag must be a list")
            occurred_on = msk_day(_parse_moment(raw.get("date")))
            revision = int(raw.get("revision") or 1)
            # a held (not yet settled) operation is visible but is not an actual
            # movement yet: it stays out of the sums until the source settles it
            held = bool(raw.get("hold"))
            if held:
                notes.append("held")
            income_account = _account_id(raw.get("incomeAccount"))
            outcome_account = _account_id(raw.get("outcomeAccount"))
            transfer = bool(
                income_account
                and outcome_account
                and income_account != outcome_account
                and income > 0
                and outcome > 0
            )
            category_id = str(tags[0]) if tags else None

            if transfer:
                pair_id = f"pair:{transaction_id}"
                out_leg = self._movement(
                    movement_id=f"{transaction_id}:out",
                    occurred_on=occurred_on,
                    amount=outcome,
                    currency=resolve(raw.get("outcomeInstrument")),
                    account=outcome_account,
                    source=True,
                    category_id=category_id,
                    revision=revision,
                    pair_id=pair_id,
                    notes=tuple(notes),
                    is_planned=held,
                )
                in_leg = self._movement(
                    movement_id=f"{transaction_id}:in",
                    occurred_on=occurred_on,
                    amount=income,
                    currency=resolve(raw.get("incomeInstrument")),
                    account=income_account,
                    source=False,
                    category_id=category_id,
                    revision=revision,
                    pair_id=pair_id,
                    notes=tuple(notes),
                    is_planned=held,
                )
                movements.extend((out_leg, in_leg))
                rows.append(
                    ZenmoneyRow(
                        transaction_id=transaction_id,
                        occurred_on=occurred_on,
                        amount_kopecks=outcome,
                        source_account=outcome_account,
                        target_account=income_account,
                        category_id=category_id,
                        currency=out_leg.currency,
                        revision=revision,
                        income_kopecks=income,
                        outcome_kopecks=outcome,
                        kind="transfer",
                        notes=tuple(notes),
                        held=held,
                    )
                )
                continue

            # one account on both sides (or only one side stated): a money move
            if income > 0 and outcome > 0:
                # the same account on both sides: the row is the net move, not a transfer
                notes.append("same_account_two_sided")
                if income > outcome:
                    amount, source, instrument = income - outcome, False, raw.get("incomeInstrument")
                    account = income_account or outcome_account
                elif outcome > income:
                    amount, source, instrument = outcome - income, True, raw.get("outcomeInstrument")
                    account = outcome_account or income_account
                else:
                    amount, source, instrument, account = 0, False, None, None
            elif income > 0:
                amount, source, instrument = income, False, raw.get("incomeInstrument")
                account = income_account or outcome_account
            else:
                amount, source, instrument = outcome, True, raw.get("outcomeInstrument")
                account = outcome_account or income_account

            if amount == 0:
                # a netted zero (both sides equal) is not a money movement
                skipped_zero += 1
                continue
            if account is None:
                # an amount without any account cannot be attributed to the contour
                raise ZenmoneyProtocolError(f"transaction {transaction_id} names no account")

            movement = self._movement(
                movement_id=transaction_id,
                occurred_on=occurred_on,
                amount=amount,
                currency=resolve(instrument),
                account=account,
                source=source,
                category_id=category_id,
                revision=revision,
                pair_id=None,
                notes=tuple(notes),
                is_planned=held,
            )
            movements.append(movement)
            rows.append(
                ZenmoneyRow(
                    transaction_id=transaction_id,
                    occurred_on=occurred_on,
                    amount_kopecks=amount,
                    source_account=movement.source_account,
                    target_account=movement.target_account,
                    category_id=category_id,
                    currency=movement.currency,
                    revision=revision,
                    income_kopecks=income,
                    outcome_kopecks=outcome,
                    kind="money",
                    notes=movement.notes,
                    held=held,
                )
            )

        accounts = self._accounts(payload.get("account") or (), resolve)
        deleted_ids.extend(self._deletions(payload.get("deletion") or ()))
        counts = {
            "rows": len(rows),
            "deleted": len(deleted_ids),
            "movements": len(movements),
            "skipped_zero": skipped_zero,
        }
        return ZenmoneyDiff(
            server_timestamp=server_timestamp,
            movements=tuple(movements),
            deleted_ids=tuple(deleted_ids),
            rows=tuple(rows),
            skipped_zero_amount=skipped_zero,
            currency_unknown=tuple(dict.fromkeys(currency_unknown)),
            instruments_seen=tuple(sorted(instruments_seen)),
            counts=counts,
            account_rows=accounts,
        )

    @staticmethod
    def _movement(
        *,
        movement_id: str,
        occurred_on: date,
        amount: int,
        currency: str,
        account: str | None,
        source: bool,
        category_id: str | None,
        revision: int,
        pair_id: str | None,
        notes: tuple[str, ...] = (),
        is_planned: bool = False,
    ) -> Movement:
        combined = list(notes)
        if currency == UNKNOWN_CURRENCY:
            combined.append("currency_unknown")
            currency = "RUB"
        return Movement(
            id=movement_id,
            occurred_on=occurred_on,
            amount_kopecks=amount,
            source_account=account if source else None,
            target_account=None if source else account,
            category_id=category_id,
            currency=currency,
            pair_id=pair_id,
            pair_kind="transfer_leg" if pair_id else None,
            is_planned=is_planned,
            source_revision=revision,
            notes=tuple(dict.fromkeys(combined)),
        )

    @staticmethod
    def _deletions(entries: Sequence[Any]) -> list[str]:
        """Hard deletions: the documented ``deletion`` array of the diff."""

        out: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("object") or "").lower() != "transaction":
                continue
            identifier = entry.get("id")
            if identifier is not None:
                out.append(str(identifier))
        return out

    @staticmethod
    def _accounts(entries: Sequence[Any], resolve: Callable[[Any], str]) -> tuple[ZenmoneyAccount, ...]:
        out: list[ZenmoneyAccount] = []
        for raw in entries:
            if not isinstance(raw, Mapping) or raw.get("id") is None:
                continue
            balance = raw.get("balance")
            account_type = raw.get("type")
            out.append(
                ZenmoneyAccount(
                    account_id=str(raw["id"]),
                    account_type=None if account_type is None else str(account_type).strip().lower(),
                    currency=resolve(raw.get("instrument")),
                    balance_kopecks=None if balance is None else _amount_kopecks(balance),
                    archived=bool(raw.get("archive")),
                    title=None if raw.get("title") is None else str(raw.get("title")),
                )
            )
        return tuple(out)


__all__ = [
    "CURRENCY_CODES",
    "DEFAULT_BASE_URL",
    "DIFF_PATH",
    "FORBIDDEN_REQUEST_KEYS",
    "LIABILITY_ACCOUNT_TYPES",
    "LIQUID_ACCOUNT_TYPES",
    "READ_REQUEST_KEYS",
    "ReadOnlyViolation",
    "UNKNOWN_CURRENCY",
    "ZenmoneyAccount",
    "ZenmoneyClient",
    "ZenmoneyDiff",
    "ZenmoneyProtocolError",
    "ZenmoneyRow",
    "account_balances",
    "currency_code",
]
