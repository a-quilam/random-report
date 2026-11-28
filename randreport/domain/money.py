"""Kopeck-exact money helpers.

Rules (docs/REQUIREMENTS.md section 6.4):
* all amounts are integer kopecks or ``Decimal`` with an explicit rounding rule;
* floats are rejected outright — they must never reach a calculation;
* an unknown amount is ``None``; zero is a confirmed zero.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Iterable

KOPEKS_PER_RUBLE = Decimal(100)
ROUNDING = ROUND_HALF_EVEN  # parsing/quantizing rule for source amounts
SHARE_ROUNDING = ROUND_HALF_UP  # display rule for percentages

Kopecks = int

_STRIP = ("\u00a0", " ", "\u202f")


class MoneyTypeError(TypeError):
    """Raised when a non-decimal type (float, bool, object) is used as money."""


class MoneyValueError(ValueError):
    """Raised when a decimal value cannot be interpreted as rubles."""


def _reject_non_decimal(value: object) -> None:
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyTypeError(f"money must be int/str/Decimal in kopecks or rubles, got {type(value).__name__}")


def _require_kopecks(value: object) -> int:
    _reject_non_decimal(value)
    if not isinstance(value, int):
        raise MoneyTypeError(f"expected integer kopecks, got {type(value).__name__}")
    return value


def _to_decimal(value: str | int | Decimal) -> Decimal:
    _reject_non_decimal(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value
        for token in _STRIP:
            text = text.replace(token, "")
        text = text.replace(",", ".")
        try:
            return Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise MoneyValueError(f"cannot read decimal value {value!r}") from exc
    raise MoneyTypeError(f"unsupported money type {type(value).__name__}")


def parse_rub(value: str | int | Decimal) -> int:
    """Parse a ruble amount (int rubles, string, Decimal) into integer kopecks.

    Rounding is ``ROUND_HALF_EVEN`` on the second decimal digit.
    """

    quantized = _to_decimal(value).quantize(Decimal("0.01"), rounding=ROUNDING)
    return int(quantized * KOPEKS_PER_RUBLE)


def format_kopecks(kopecks: int) -> str:
    """Render integer kopecks as a plain decimal ruble string, e.g. ``1629.69``."""

    value = _require_kopecks(kopecks)
    sign = "-" if value < 0 else ""
    rubles, rest = divmod(abs(value), 100)
    return f"{sign}{rubles}.{rest:02d}"


def format_rub(kopecks: int) -> str:
    return f"{format_kopecks(kopecks)} \u20bd"


def format_percent(value: str | int | Decimal | None, *, digits: int = 1) -> str | None:
    """A percentage in the leaflet's one Russian form, e.g. ``3,2``; ``None`` stays ``None``.

    The single place a percentage becomes text (docs/PERCENT_FORMAT.md): a comma as
    the decimal separator, exactly ``digits`` digits after it, rounded with
    ``SHARE_ROUNDING`` — the same convention the money formatter follows. The result
    is the number alone: no ``%``, no words, the caller adds them
    (``render.percent_text``). An unreadable value is ``None``, exactly like an
    unknown share, so the display layer never prints an unparsed token as if it were
    a percentage. Type discipline stays strict: a float or a bool raises, as
    everywhere in this module.
    """

    if value is None:
        return None
    text = value.strip().rstrip("%").strip() if isinstance(value, str) else value
    if isinstance(text, str) and not text:
        return None
    try:
        number = _to_decimal(text)
    except MoneyValueError:
        return None
    quantum = Decimal(1).scaleb(-digits)
    rendered = number.quantize(quantum, rounding=SHARE_ROUNDING)
    return f"{rendered:.{digits}f}".replace(".", ",")


def sum_kopecks(values: Iterable[int]) -> int:
    """Sum integer kopecks, refusing anything that is not an integer."""

    total = 0
    for value in values:
        total += _require_kopecks(value)
    return total


def share_percent(part_kopecks: int, whole_kopecks: int, digits: int = 1) -> Decimal | None:
    """Percentage of ``whole`` represented by ``part``.

    ``None`` when the base is zero: a share of nothing is not applicable, it is
    not 0% and not infinity.
    """

    part = _require_kopecks(part_kopecks)
    whole = _require_kopecks(whole_kopecks)
    if whole == 0:
        return None
    quantum = Decimal(1).scaleb(-digits)
    return (Decimal(part) * 100 / Decimal(whole)).quantize(quantum, rounding=SHARE_ROUNDING)


def apply_rate(kopecks: int, rate: Decimal) -> int:
    """Convert an amount in a foreign minor unit to ruble kopecks with an explicit rate."""

    amount = _require_kopecks(kopecks)
    if not isinstance(rate, Decimal):
        raise MoneyTypeError("rate must be Decimal")
    return int((Decimal(amount) * rate).quantize(Decimal(1), rounding=SHARE_ROUNDING))
