"""Rule-based extractor for unstructured text (txt and PDF text).

This is the offline extractor: it makes the system runnable without an LLM and
serves as the fallback when the LLM is unavailable. The LLM extractor handles
layouts these rules cannot. A value that is absent from the source stays None.
"""

import re

from .models import Invoice, LineItem
from .normalize import (
    canonical_invoice_number,
    is_relative_date,
    parse_date,
    parse_money,
    split_item_note,
    split_vendor,
)

_MONEY = r"[\d,.Oo]*\d[\d,.Oo]*"
_DATE_TOKEN = (
    r"(?:\d{4}-\d{2}-\d{2}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}-[A-Za-z]{3}-[\dOo]{4}"
    r"|\d{1,2}/\d{1,2}/\d{4})"
)

DATE_TOKEN = _DATE_TOKEN

_INV_NUMBER = [
    re.compile(r"\bINV[-\s]?(\d{3,})", re.I),
    re.compile(r"\bInv(?:oice)?\s*(?:#|no\.?|number)\s*:?\s*(\d{3,})", re.I),
]
_VENDOR = re.compile(r"^\s*(?:Vendor|Vndr)\s*:\s*(.+?)\s*$", re.I | re.M)
_FROM = re.compile(r"^\s*FROM\s*:\s*(.+?)\s*$", re.I | re.M)
_DATE = re.compile(r"(?<![A-Za-z])(?<!Due )(?:Date|Dt)\s*:\s*(" + _DATE_TOKEN + ")", re.I)
_DUE = re.compile(r"(?<![A-Za-z])Due(?:\s*(?:Date|Dt))?\s*:\s*(.+)", re.I)
_TERMS = re.compile(r"(?:Payment\s*Terms|Pymnt\s*Terms|Terms)\s*:\s*(.+)", re.I)
_NOTES = re.compile(r"^\s*Notes?\s*:\s*(.+?)\s*$", re.I | re.M)
_SUBTOTAL = re.compile(r"(?<![A-Za-z])Subtotal\s*:\s*\$?\s*(" + _MONEY + ")", re.I)
_TAX = re.compile(
    r"(?<![A-Za-z])(?:Sales\s+)?Tax(?:\s*\([\d.]+%\))?\s*:\s*\$?\s*(" + _MONEY + ")", re.I
)
_SHIPPING = re.compile(r"(?<![A-Za-z])Shipping\s*:\s*\$?\s*(" + _MONEY + ")", re.I)
_TOTAL = re.compile(
    r"(?<![A-Za-z])(?:Grand\s+Total|Total\s+Amount|Total|Amt)\s*:\s*\$?\s*(" + _MONEY + ")",
    re.I,
)
_PO_REF = re.compile(r"\bPO-\d+\b")

# "WidgetA    qty: 10    unit price: $250.00" / "GadgetX  qty 20   @ $750 ea"
_ITEM_QTY = re.compile(
    r"^\s*([A-Za-z][\w ]*?)\s+qty:?\s*(\d+)\s+(?:@|unit\s*price:?)\s*\$\s?(" + _MONEY + ")",
    re.I,
)
# "- SuperGizmo       x12     $400.00 each"
_ITEM_X = re.compile(r"^\s*-\s*(\S.*?)\s+x(\d+)\s+\$\s?(" + _MONEY + ")", re.I)
# "WidgetA (rush order)   4   $300.00   $1,200.00   [note]"
_ITEM_ROW = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 ]*?(?:\s*\([^)]*\))?)\s+(\d+)\s+\$\s?("
    + _MONEY
    + r")\s+\$\s?("
    + _MONEY
    + r")\s*(.*?)\s*$"
)


def _first(patterns, text):
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match
    return None


def _extract_vendor(text: str) -> tuple[str | None, list[str], str | None]:
    """Returns (vendor, former_names, sender_email)."""
    sender = None
    match = _VENDOR.search(text)
    raw = None
    if match:
        raw = match.group(1)
        # PDFs flatten columns: "Atlas Industrial Supply Due: 2026-03-24".
        raw = re.split(r"\s+(?:Due|Date|Terms)\b", raw, maxsplit=1)[0].strip()
    else:
        from_match = _FROM.search(text)
        if from_match:
            value = from_match.group(1)
            if "@" in value:
                sender = value
            else:
                raw = value
                tail = text[from_match.end():].lstrip("\n")
                continuation = re.match(r"\s*(\(\s*formerly[^)]*\))", tail, re.I)
                if continuation:
                    raw = f"{raw} {continuation.group(1)}"
    if not raw:
        return None, [], sender
    vendor, former = split_vendor(raw)
    return vendor or None, former, sender


def _extract_items(text: str) -> list[LineItem]:
    items = []
    for line in text.splitlines():
        match = _ITEM_QTY.match(line) or _ITEM_X.match(line)
        if match:
            name, note = split_item_note(match.group(1))
            items.append(
                LineItem(item=name, quantity=int(match.group(2)),
                         unit_price=parse_money(match.group(3)), note=note)
            )
            continue
        match = _ITEM_ROW.match(line)
        if match:
            name, name_note = split_item_note(match.group(1))
            note = match.group(5).strip() or name_note
            items.append(
                LineItem(item=name, quantity=int(match.group(2)),
                         unit_price=parse_money(match.group(3)),
                         amount=parse_money(match.group(4)), note=note)
            )
    return items


def extract_invoice(text: str, source_file: str | None = None) -> Invoice:
    inv = Invoice(source_file=source_file, source_text=text)

    match = _first(_INV_NUMBER, text)
    inv.invoice_number = canonical_invoice_number(match.group(1)) if match else None

    inv.vendor, former, sender = _extract_vendor(text)
    if former:
        inv.extras["former_names"] = former
    if sender:
        inv.extras["sender"] = sender

    match = _DATE.search(text)
    if match:
        inv.date = parse_date(match.group(1))

    for line in text.splitlines():
        match = _DUE.search(line)
        if not match:
            continue
        value = match.group(1)
        date_match = re.search(_DATE_TOKEN, value)
        if date_match:
            inv.due_date = parse_date(date_match.group(0))
        else:
            word = value.strip().split()[0].lower().rstrip(".,") if value.strip() else ""
            inv.due_date_text = word if is_relative_date(word) else value.strip()
        break

    inv.line_items = _extract_items(text)

    for attr, pattern in (("subtotal", _SUBTOTAL), ("tax", _TAX), ("shipping", _SHIPPING)):
        match = pattern.search(text)
        if match:
            setattr(inv, attr, parse_money(match.group(1)))
    totals = _TOTAL.findall(text)
    if totals:
        inv.total = parse_money(totals[-1])

    match = _TERMS.search(text)
    if match:
        inv.payment_terms = match.group(1).strip()
    match = _NOTES.search(text)
    if match:
        inv.notes = match.group(1).strip()
    match = _PO_REF.search(text)
    if match:
        inv.extras["po_reference"] = match.group(0)

    if re.search(r"\bEUR\b|€", text):
        inv.currency = "EUR"
    return inv
