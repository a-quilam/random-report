"""Query allowlist for the public surface.

Only whole months, whole years and "all time" are public periods. Query strings
that would isolate a single day, a single account, a single occurrence or a
single sensitive category are rejected before any data is read, so no unfiltered
entity can be handed out "by accident" (docs/REQUIREMENTS.md sections 6.6, 10).
"""

from __future__ import annotations

from typing import Mapping

from ..domain.timeframes import (
    ALL_TIME,
    MONTH,
    YEAR,
    Period,
    UnsafePeriodError,
    parse_period,
)

ALLOWED_QUERY_PARAMS = frozenset({"period", "granularity", "unit"})
ALLOWED_GRANULARITY = frozenset({"month", "year"})
ALLOWED_UNITS = frozenset({"RUB_kopecks"})
REQUIRED_QUERY_PARAMS = frozenset({"period"})


class UnsafeQueryError(ValueError):
    """A public request that must not be answered."""


def validate_params(params: Mapping[str, str] | None) -> dict[str, str]:
    """Validate query parameters against the allowlist; raise on anything else."""

    provided = dict(params or {})
    unknown = sorted(set(provided) - ALLOWED_QUERY_PARAMS)
    if unknown:
        raise UnsafeQueryError(f"parameters outside the public allowlist: {unknown}")
    normalized = {str(key): str(value) for key, value in provided.items()}
    # the period value itself is validated here so that a single gate rejects
    # day-level, partial or otherwise unsafe periods before any data is read
    try:
        parse_period(normalized["period"])
    except KeyError:
        pass
    except UnsafePeriodError as exc:
        raise UnsafeQueryError(str(exc)) from exc
    missing = sorted(REQUIRED_QUERY_PARAMS - set(normalized))
    if missing:
        raise UnsafeQueryError(f"missing required public parameters: {missing}")
    granularity = normalized.get("granularity")
    if granularity is not None and granularity not in ALLOWED_GRANULARITY:
        raise UnsafeQueryError(f"unsupported granularity {granularity!r}")
    unit = normalized.get("unit")
    if unit is not None and unit not in ALLOWED_UNITS:
        raise UnsafeQueryError(f"unsupported unit {unit!r}")
    return normalized


def resolve_period(params: Mapping[str, str] | None) -> Period:
    """Resolve the public period of a request; day-level periods are refused."""

    normalized = validate_params(params)
    try:
        period = parse_period(normalized["period"])
    except UnsafePeriodError as exc:
        raise UnsafeQueryError(str(exc)) from exc
    granularity = normalized.get("granularity")
    if granularity is not None and period.kind in (MONTH, YEAR) and granularity != period.kind:
        raise UnsafeQueryError(f"granularity {granularity!r} does not match period {normalized['period']!r}")
    return period


def check_period_allowed(period: Period) -> None:
    if period.kind == ALL_TIME:
        return
    if period.start is None or period.end is None:  # pragma: no cover - defensive
        raise UnsafeQueryError("period without bounds")
    if period.start.day != 1:
        raise UnsafeQueryError("public periods start on the first day of a month")


def calendar_grid_granularity(period: Period) -> str:
    """Daily buckets only for a single month; longer periods stay monthly."""

    return "day" if period.kind == MONTH else "month"
