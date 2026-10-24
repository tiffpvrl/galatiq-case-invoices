"""Normalization helpers: money, dates, OCR repair, and identifier keys.

Item and vendor matching normalizes case, whitespace and punctuation only.
There is deliberately no fuzzy matching: "WidgetC" is one edit from "WidgetA",
so fuzzy matching would turn an unknown item into a pass.
"""

import datetime as dt
import re

# Letter O/o next to a digit, or l/I between digits, is an OCR confusable.
_OCR_O = re.compile(r"(?<=\d)[Oo]|[Oo](?=\d)")
_OCR_L = re.compile(r"(?<=\d)[lI](?=\d)")

RELATIVE_DATE_WORDS = ("yesterday", "today", "tomorrow")


def repair_ocr(text: str) -> str:
    """Repair digit-like letters in numeric contexts: '3,500.O0' -> '3,500.00'."""
    return _OCR_L.sub("1", _OCR_O.sub("0", text))


def parse_money(value) -> float | None:
    """'$3,500.O0' -> 3500.0; '(250)' -> -250.0; None/garbage -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = repair_ocr(str(value)).strip()
    negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned or cleaned == ".":
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%b %d %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%m/%d/%Y",
)

_SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/\d{4}$")


def is_relative_date(text: str | None) -> bool:
    return bool(text) and text.strip().lower() in RELATIVE_DATE_WORDS


def parse_date(text) -> dt.date | None:
    """Parse the date formats seen in invoices. Slash dates are read as US
    month/day/year. Returns None for relative words ('yesterday') and
    anything unparseable; the caller keeps the raw text."""
    if text is None:
        return None
    if isinstance(text, dt.date):
        return text
    raw = repair_ocr(str(text)).strip().rstrip(".")
    if not raw or is_relative_date(raw):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def date_is_ambiguous(text: str | None) -> bool:
    """True for slash dates where day and month could swap (03/04/2026)."""
    if not text:
        return False
    match = _SLASH_DATE.match(text.strip())
    return bool(match) and int(match.group(1)) <= 12 and int(match.group(2)) <= 12 \
        and match.group(1) != match.group(2)


def canonical_invoice_number(value) -> str | None:
    """'INV 1012', 'inv-1012' and '1012' all become 'INV-1012'."""
    if value is None:
        return None
    match = re.search(r"(\d+)", str(value))
    return f"INV-{match.group(1)}" if match else None


def _alnum_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def item_key(name: str) -> str:
    """'Widget A' / 'widgeta' / 'WidgetA (rush order)' -> 'widgeta'."""
    return _alnum_key(re.sub(r"\([^)]*\)", "", name))


def vendor_key(name: str) -> str:
    return _alnum_key(name)


def split_item_note(name: str) -> tuple[str, str | None]:
    """'WidgetA (rush order)' -> ('WidgetA', 'rush order')."""
    match = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", name.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip() or None
    return name.strip(), None


def split_vendor(name: str) -> tuple[str, list[str]]:
    """'QuickShip Distributers (formerly FastShip Ltd.)' ->
    ('QuickShip Distributers', ['FastShip Ltd.'])."""
    match = re.match(r"^(.*?)\s*\(\s*formerly\s+(.+?)\s*\)\s*$", name.strip(), re.I)
    if match:
        return match.group(1).strip(), [match.group(2).strip()]
    return name.strip(), []
