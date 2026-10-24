"""File readers: one deterministic reader per format.

json, csv and xml are structured, so a parser is exact, free and testable and
never touches an LLM. txt and PDF go through the text extractor. Any failure
raises ReadError so the caller can fall back to the LLM extractor.
"""

import csv
import io
import json
from pathlib import Path

from defusedxml import ElementTree

from .models import Invoice, LineItem
from .normalize import (
    canonical_invoice_number,
    date_is_ambiguous,
    is_relative_date,
    parse_date,
    parse_money,
    split_vendor,
)
from .text_extractor import extract_invoice

STRUCTURED = {".json", ".csv", ".xml"}
TEXTUAL = {".txt", ".pdf"}


class ReadError(Exception):
    """The file could not be read into an Invoice."""


def _set_date(inv: Invoice, field: str, raw) -> None:
    """Set a date field; keep the raw text when it is relative or unparseable."""
    if raw in (None, ""):
        return
    parsed = parse_date(raw)
    if parsed:
        setattr(inv, field, parsed)
        if date_is_ambiguous(str(raw)):
            inv.extras[f"{field}_ambiguous"] = str(raw)
    elif field == "due_date":
        inv.due_date_text = str(raw)


def _vendor(inv: Invoice, raw) -> None:
    if isinstance(raw, dict):
        if raw.get("address"):
            inv.extras["vendor_address"] = raw["address"]
        raw = raw.get("name")
    if raw and str(raw).strip():
        inv.vendor, former = split_vendor(str(raw))
        if former:
            inv.extras["former_names"] = former


def _read_json(path: Path) -> Invoice:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReadError(f"malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReadError("JSON root is not an object")

    inv = Invoice(source_file=path.name, source_text=text)
    inv.invoice_number = canonical_invoice_number(data.get("invoice_number"))
    _vendor(inv, data.get("vendor"))
    _set_date(inv, "date", data.get("date"))
    _set_date(inv, "due_date", data.get("due_date"))
    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict) or "item" not in raw:
            raise ReadError("line item without an 'item' field")
        inv.line_items.append(
            LineItem(item=str(raw["item"]), quantity=raw.get("quantity", 0),
                     unit_price=parse_money(raw.get("unit_price")),
                     amount=parse_money(raw.get("amount")), note=raw.get("note"))
        )
    inv.subtotal = parse_money(data.get("subtotal"))
    inv.tax = parse_money(data.get("tax_amount"))
    inv.shipping = parse_money(data.get("shipping"))
    inv.total = parse_money(data.get("total"))
    inv.currency = (data.get("currency") or "USD").upper()
    inv.payment_terms = data.get("payment_terms") or None
    inv.notes = data.get("notes") or None
    inv.revision = data.get("revision")
    if data.get("tax_rate") is not None:
        inv.extras["tax_rate"] = data["tax_rate"]
    return inv


def _read_csv(path: Path) -> Invoice:
    text = path.read_text(encoding="utf-8")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ReadError("empty CSV")
    inv = Invoice(source_file=path.name, source_text=text)

    if [c.strip().lower() for c in rows[0][:2]] == ["field", "value"]:
        _csv_key_value(inv, rows[1:])
    else:
        _csv_table(inv, rows)
    if not inv.line_items and inv.invoice_number is None:
        raise ReadError("CSV layout not recognised")
    return inv


def _csv_key_value(inv: Invoice, rows) -> None:
    current: dict | None = None

    def flush():
        nonlocal current
        if current and "item" in current:
            inv.line_items.append(
                LineItem(item=current["item"],
                         quantity=parse_money(current.get("quantity")) or 0,
                         unit_price=parse_money(current.get("unit_price")))
            )
        current = None

    for row in rows:
        if len(row) < 2:
            continue
        key, value = row[0].strip().lower(), row[1].strip()
        if key == "item":
            flush()
            current = {"item": value}
        elif key in ("quantity", "unit_price") and current is not None:
            current[key] = value
        elif key == "invoice_number":
            inv.invoice_number = canonical_invoice_number(value)
        elif key == "vendor":
            _vendor(inv, value)
        elif key in ("date", "due_date"):
            _set_date(inv, key, value)
        elif key == "subtotal":
            inv.subtotal = parse_money(value)
        elif key == "tax":
            inv.tax = parse_money(value)
        elif key == "total":
            inv.total = parse_money(value)
        elif key == "payment_terms":
            inv.payment_terms = value or None
        elif key == "currency":
            inv.currency = value.upper() or "USD"
    flush()


def _csv_table(inv: Invoice, rows) -> None:
    header = [c.strip().lower() for c in rows[0]]
    col = {name: i for i, name in enumerate(header)}
    needed = {"invoice number", "item", "qty", "unit price"}
    if not needed <= col.keys():
        raise ReadError(f"unrecognised CSV header: {rows[0]}")

    def cell(row, name):
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in rows[1:]:
        label = row[-2].strip().lower().rstrip(":") if len(row) >= 2 else ""
        if cell(row, "invoice number"):
            if inv.invoice_number is None:
                inv.invoice_number = canonical_invoice_number(cell(row, "invoice number"))
                _vendor(inv, cell(row, "vendor"))
                _set_date(inv, "date", cell(row, "date"))
                _set_date(inv, "due_date", cell(row, "due date"))
            inv.line_items.append(
                LineItem(item=cell(row, "item"),
                         quantity=parse_money(cell(row, "qty")) or 0,
                         unit_price=parse_money(cell(row, "unit price")),
                         amount=parse_money(cell(row, "line total")))
            )
        elif label.startswith("subtotal"):
            inv.subtotal = parse_money(row[-1])
        elif label.startswith("tax"):
            inv.tax = parse_money(row[-1])
        elif label.startswith("shipping"):
            inv.shipping = parse_money(row[-1])
        elif label.startswith("total"):
            inv.total = parse_money(row[-1])


def _read_xml(path: Path) -> Invoice:
    text = path.read_text(encoding="utf-8")
    try:
        root = ElementTree.fromstring(text)
    except Exception as exc:  # ParseError or a defusedxml safety error
        raise ReadError(f"malformed XML: {exc}") from exc

    def get(node, tag):
        found = node.find(tag) if node is not None else None
        return found.text.strip() if found is not None and found.text else None

    header = root.find("header")
    inv = Invoice(source_file=path.name, source_text=text)
    inv.invoice_number = canonical_invoice_number(get(header, "invoice_number"))
    _vendor(inv, get(header, "vendor"))
    _set_date(inv, "date", get(header, "date"))
    _set_date(inv, "due_date", get(header, "due_date"))
    inv.currency = (get(header, "currency") or "USD").upper()
    for node in root.findall("./line_items/item"):
        name = get(node, "name")
        if not name:
            raise ReadError("XML line item without a name")
        inv.line_items.append(
            LineItem(item=name, quantity=parse_money(get(node, "quantity")) or 0,
                     unit_price=parse_money(get(node, "unit_price")),
                     amount=parse_money(get(node, "amount")))
        )
    totals = root.find("totals")
    inv.subtotal = parse_money(get(totals, "subtotal"))
    inv.tax = parse_money(get(totals, "tax_amount"))
    inv.total = parse_money(get(totals, "total"))
    if get(totals, "tax_rate"):
        inv.extras["tax_rate"] = parse_money(get(totals, "tax_rate"))
    inv.payment_terms = get(root, "payment_terms")
    return inv


def _read_pdf_text(path: Path) -> str:
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:
        raise ReadError(f"unreadable PDF: {exc}") from exc
    if not text.strip():
        raise ReadError("PDF has no text layer (scanned image; needs OCR)")
    return text


def read_source_text(path: str | Path) -> str:
    """Raw text of any supported file, for the LLM extractor and the grounding check."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return _read_pdf_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def read_text_invoice(path: str | Path) -> Invoice:
    """Rule-based extraction from txt/pdf text (the offline extractor)."""
    path = Path(path)
    return extract_invoice(read_source_text(path), source_file=path.name)


def read_invoice(path: str | Path) -> Invoice:
    """Format router. Structured files use deterministic parsers; txt/pdf use
    the rule-based text extractor. Raises ReadError on failure."""
    path = Path(path)
    suffix = path.suffix.lower()
    if not path.is_file():
        raise ReadError(f"file not found: {path}")
    if suffix == ".json":
        return _read_json(path)
    if suffix == ".csv":
        return _read_csv(path)
    if suffix == ".xml":
        return _read_xml(path)
    if suffix in TEXTUAL:
        return read_text_invoice(path)
    raise ReadError(f"unsupported format: {suffix or '(none)'}")
