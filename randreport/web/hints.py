"""Accessible rendering of the approved category names, hints and overrides.

The owner approves, per category, a public *name* and optionally a short *hint*
(``docs/ALLOCATION.md`` section 5).  The contract the UI must honour is:

* the public name is always the confirmed label — the UI never invents one;
* a hint is **optional** and must be reachable by *touch* **and** *keyboard*.
  The public site's CSP allows no JavaScript, so a hover-only tooltip would fail
  both tests; the disclosure is a native ``<details>`` element whose ``<summary>``
  is focusable and tap-activatable without a script.
* the allocation explanation the reader sees is built from the same
  :class:`~randreport.domain.dto.AllocationDTO` the JSON and CSV carry, so the
  three views can never disagree.
* every amount is dressed the way the rest of the leaflet dresses it
  (:func:`rub_from_kopecks`): space thousands, comma kopecks, ``₽``.  The block
  must not invent a second money convention on the same page.

This module is intentionally free of any import from the SSR layer except the
domain DTOs and the domain money helper, so it can never create an import cycle
with ``render``: ``render`` imports *this* module, never the other way round.
"""

from __future__ import annotations

from html import escape as _escape
from typing import Any

from ..domain.dto import AllocationDTO, CategoryDTO
from ..domain.money import format_kopecks


def e(value: Any) -> str:
    return _escape("" if value is None else str(value))


def hint_disclosure(hint: str | None, *, label: str) -> str:
    """The optional hint as a keyboard- and touch-reachable disclosure.

    Returns an empty string when no hint was approved: the UI must not invent
    one, and an empty disclosure would be noise for a screen reader.
    """

    text = (hint or "").strip()
    if not text:
        return ""
    return (
        f'<details class="hint-disclosure"><summary aria-label="Подсказка: {e(label)}">подсказка</summary>'
        f'<span class="hint-text">{e(text)}</span></details>'
    )


def category_label(category: CategoryDTO) -> str:
    """The confirmed name, with its approved hint next to it when there is one."""

    name = e(category.label)
    return f"{name} {hint_disclosure(category.hint, label=category.label)}".rstrip()


def rub_from_kopecks(kopecks: Any) -> str:
    """Kopecks as Russian money: space thousands, comma kopecks — ``9 000,00 ₽``.

    The leaflet's one money convention; ``render.rub_from_kopecks`` delegates
    here so the categories table and the allocation block can never disagree
    about how a number is dressed.
    """

    if not isinstance(kopecks, int) or isinstance(kopecks, bool):
        return "нет данных"
    raw = format_kopecks(kopecks)
    sign = "-" if raw.startswith("-") else ""
    whole, _, cents = raw.lstrip("-").partition(".")
    grouped = f"{int(whole):,}".replace(",", " ")
    return f"{sign}{grouped},{cents} ₽"


#: Backwards-compatible short name used inside this module.
rub = rub_from_kopecks


def allocation_block(allocation: AllocationDTO | None) -> str:
    """A plain-language explanation of the applied overrides, or nothing."""

    if allocation is None or not allocation.applied:
        return ""
    parts: list[str] = ["<h2>Правила распределения подвала</h2>"]
    if allocation.excluded_categories:
        parts.append(
            f"<p>Из отчёта исключены категории: {allocation.excluded_categories} — их суммы не входят "
            "ни в один итог.</p>"
        )
    if allocation.excluded_operations:
        parts.append(
            f"<p>Из отчёта обратимо исключены отдельные операции: {allocation.excluded_operations}.</p>"
        )
    if allocation.shared_expenses_after_kopecks:
        parts.append(
            "<p>Общие расходы и ЖКУ показаны долей подвала: "
            f"из {rub(allocation.shared_expenses_before_kopecks)} учтено "
            f"{rub(allocation.shared_expenses_after_kopecks)} по месячным суммам в целых копейках.</p>"
        )
    if allocation.rent_applied:
        parts.append(
            f"<p>Аренда показана согласованной суммой {rub(allocation.computed_rent_kopecks)}; "
            f"фактический банковский платёж {rub(allocation.bank_rent_kopecks)} в итоги не добавляется.</p>"
        )
        rows = [
            (month.month, rub(month.computed_kopecks), rub(month.bank_kopecks))
            for month in allocation.rent_months
            if month.applied
        ]
        if rows:
            head = "".join(
                f'<th scope="col">{e(column)}</th>'
                for column in ("Месяц", "Расчётная аренда", "Банковский платёж")
            )
            body = "".join(
                f'<tr><th scope="row">{e(month)}</th><td class="num">{e(computed)}</td>'
                f'<td class="num">{e(bank)}</td></tr>'
                for month, computed, bank in rows
            )
            # the leaflet's own table furniture (a scrollable wrapper, a caption,
            # a row header) — the same as render.data_table emits
            parts.append(
                '<div class="table-wrap"><table><caption>Расчётная аренда и фактический платёж по месяцам</caption>'
                f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
            )
    if allocation.corrections_positive_kopecks or allocation.corrections_negative_kopecks:
        parts.append(
            "<p>Корректировки подвала учтены раздельно: положительные "
            f"{rub(allocation.corrections_positive_kopecks)}, отрицательные "
            f"{rub(allocation.corrections_negative_kopecks)}.</p>"
        )
    if allocation.corrections_unclassified:
        parts.append(
            "<p>Часть корректировок относится к статье без подтверждённого класса: им не присвоен "
            "выдуманный класс, они показаны как неопознанные.</p>"
        )
    if allocation.rent_differs:
        parts.append(
            "<p>Расчётная аренда отличается от банковского платежа: оба числа показаны рядом, "
            "двойного учёта нет.</p>"
        )
    if len(parts) == 1:
        return ""
    return "".join(parts)


__all__ = [
    "allocation_block",
    "category_label",
    "e",
    "hint_disclosure",
    "rub",
    "rub_from_kopecks",
]
