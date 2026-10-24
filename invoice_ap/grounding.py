"""Grounding check: is every extracted value actually in the source?

Plain code, not an LLM: a normalized substring/number match is deterministic
and a model judging another model's output is weaker than string comparison.
Absent fields are never errors (the source simply lacks them); only a value
that was extracted but does not appear in the source is flagged, which is
what hallucination looks like. Error messages are fed back to the extractor.
"""

import re

from .models import Invoice
from .normalize import item_key, parse_date, parse_money, repair_ocr, vendor_key
from .text_extractor import DATE_TOKEN

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(source: str) -> set[float]:
    found = set()
    for token in _NUMBER.findall(repair_ocr(source)):
        value = parse_money(token)
        if value is not None:
            found.add(round(value, 2))
    return found


def _dates(source: str) -> set:
    return {d for tok in re.findall(DATE_TOKEN, repair_ocr(source)) if (d := parse_date(tok))}


def check_grounding(inv: Invoice, source: str) -> list[str]:
    """Return one message per extracted value that cannot be found in the source."""
    errors: list[str] = []
    alnum = re.sub(r"[^a-z0-9]", "", source.lower())
    numbers = _numbers(source)
    dates = _dates(source)

    def need_number(label: str, value) -> None:
        if value is not None and round(abs(value), 2) not in numbers and round(value, 2) not in numbers:
            errors.append(f"{label} {value} does not appear in the source")

    if inv.invoice_number:
        digits = re.sub(r"\D", "", inv.invoice_number)
        if digits not in re.findall(r"\d+", source):
            errors.append(f"invoice_number {inv.invoice_number} does not appear in the source")
    if inv.vendor and vendor_key(inv.vendor) not in alnum:
        errors.append(f"vendor '{inv.vendor}' does not appear in the source")
    for label, value in (("date", inv.date), ("due_date", inv.due_date)):
        if value is not None and value not in dates:
            errors.append(f"{label} {value} does not appear in the source")
    for li in inv.line_items:
        if item_key(li.item) not in alnum:
            errors.append(f"line item '{li.item}' does not appear in the source")
        need_number(f"quantity of {li.item}", li.quantity)
        need_number(f"unit_price of {li.item}", li.unit_price)
        need_number(f"amount of {li.item}", li.amount)
    for label in ("subtotal", "tax", "shipping", "total"):
        need_number(label, getattr(inv, label))
    return errors
