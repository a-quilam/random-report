"""Privacy guard for every public payload (docs/REQUIREMENTS.md sections 6.6, 7, 10).

The guard works on the serialised structure, not on the intent of the code that
built it: forbidden key names (raw ids, counterparties, private fields) and
forbidden value shapes (source account ids, occurrence ids, raw category ids,
calendar ids, e-mail addresses) are detected anywhere in the tree.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from ..domain.dto import StrictDTO, encode_public_value

FORBIDDEN_KEYS = frozenset(
    {
        "account_id",
        "account_ids",
        "transaction_id",
        "transaction_ids",
        "counterparty",
        "counterparty_title",
        "invoice_id",
        "requisites",
        "outcome_account",
        "income_account",
        "occurrence_id",
        "occurrence_ids",
        "calendar_id",
        "series_id",
        "subcalendar_id",
        "subcalendar_ids",
        "private_title",
        "donor_name",
        "donor_aliases",
        "phone",
        "comment",
        "raw_payload",
        "tag_id",
        "chat_id",
        "message_id",
        "admin_members",
        "start_utc",
        "end_utc",
    }
)

FORBIDDEN_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:acc|occ|cat|sub|cal|pair|tx)_[A-Za-z0-9_]+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)


class PrivacyLeakError(ValueError):
    """A public payload contained private data."""


def find_leaks(node: Any, *, extra_forbidden: Iterable[str] = (), path: str = "$") -> tuple[str, ...]:
    """Return the paths of every forbidden key or value found in ``node``."""

    forbidden = FORBIDDEN_KEYS | frozenset(extra_forbidden)
    found: list[str] = []

    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}"
            if str(key).lower() in forbidden:
                found.append(f"{child}:forbidden_key")
            found.extend(find_leaks(value, extra_forbidden=extra_forbidden, path=child))
    elif isinstance(node, (list, tuple, set, frozenset)):
        for index, item in enumerate(node):
            found.extend(find_leaks(item, extra_forbidden=extra_forbidden, path=f"{path}[{index}]"))
    elif isinstance(node, str):
        match = FORBIDDEN_VALUE_RE.search(node)
        if match:
            found.append(f"{path}:forbidden_value({match.group(0)})")
    return tuple(found)


def assert_no_leaks(node: Any, *, extra_forbidden: Iterable[str] = ()) -> None:
    """Raise :class:`PrivacyLeakError` when a payload would leak private data."""

    leaks = find_leaks(node, extra_forbidden=extra_forbidden)
    if leaks:
        raise PrivacyLeakError("public payload leaks private data: " + "; ".join(leaks))


def public_json(node: Any) -> str:
    """Serialise a public payload to JSON, refusing floats and private data."""

    payload = node.to_public_dict() if isinstance(node, StrictDTO) else encode_public_value(node)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert_no_leaks(payload)
    return text
