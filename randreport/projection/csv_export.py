"""CSV export of public aggregates (docs/REQUIREMENTS.md sections 3.2, 6.6, 10).

Only category rows of whole calendar months or whole years. No transaction ids,
no accounts, no counterparties, no daily sums, no closed calendar data; the
columns are a fixed allowlist, so a non-standard request cannot widen them.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Mapping

from ..domain.dto import MoneyDTO
from .privacy import assert_no_leaks
from .query import ALLOWED_GRANULARITY, UnsafeQueryError, resolve_period, validate_params

# Spreadsheet formula triggers (OWASP CSV injection). A public category label can
# carry the text of a source category, so a label beginning with one of these
# characters could execute when the file is opened in a spreadsheet program.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _csv_cell(value: Any) -> str:
    """Serialise one cell, neutralising a spreadsheet formula trigger.

    A value that merely looks like a negative number (``-40000.00``) stays a
    number; any other cell whose first non-space character is a formula trigger
    is prefixed with a single quote, so the text is displayed literally instead
    of being evaluated as a formula.
    """

    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip()
    if stripped and stripped[0] in _FORMULA_TRIGGERS and not _PLAIN_NUMBER_RE.match(text.strip()):
        return "'" + text
    return text

CSV_HEADER = (
    "period",
    "category_key",
    "category_label",
    "bucket",
    "direction",
    "amount_kopecks",
    "amount_rub",
    "share_percent",
    "unit",
    "as_of",
    "release_id",
    "completeness",
)

CSV_KINDS = {"month", "year"}


def export_categories_csv(payload: MoneyDTO, *, params: Mapping[str, str]) -> str:
    """Render the category table of one released period as CSV."""

    normalized = validate_params(params)
    period = resolve_period(normalized)
    if period.kind not in CSV_KINDS:
        raise UnsafeQueryError("CSV export supports whole calendar months and whole years only")
    if normalized["period"] != payload.period.requested:
        raise UnsafeQueryError("requested period does not match the published release period")
    granularity = normalized.get("granularity")
    if granularity is not None and granularity not in ALLOWED_GRANULARITY:
        raise UnsafeQueryError(f"unsupported granularity {granularity!r}")
    unit = normalized.get("unit")
    if unit is not None and unit != payload.unit:
        raise UnsafeQueryError("requested unit does not match the release unit")

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for category in payload.categories:
        writer.writerow(
            tuple(
                _csv_cell(cell)
                for cell in (
                    payload.period.requested,
                    category.key,
                    category.label,
                    category.bucket,
                    category.direction,
                    category.amount_kopecks,
                    category.amount_rub,
                    category.share_percent or "",
                    payload.unit,
                    payload.as_of.isoformat(),
                    payload.release_id,
                    payload.completeness,
                )
            )
        )
    text = buffer.getvalue()
    assert_no_leaks({"csv": text})
    return text
