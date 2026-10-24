"""The processed-invoice ledger: content hashing and recording."""

import hashlib
import json
import sqlite3

from .models import Invoice, Outcome
from .normalize import item_key, vendor_key


def content_hash(inv: Invoice) -> str:
    """Hash over normalized fields, not file bytes, so the same invoice arriving
    as a PDF and as JSON produces the same hash."""
    canonical = {
        "number": inv.invoice_number,
        "vendor": vendor_key(inv.vendor or ""),
        "currency": inv.currency,
        "total": round(inv.total, 2) if inv.total is not None else None,
        "lines": sorted(
            (item_key(li.item), li.quantity, round(li.unit_price or 0, 2))
            for li in inv.line_items
        ),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


def record(
    conn: sqlite3.Connection,
    run_id: str,
    inv: Invoice,
    status: Outcome,
    reasons: list[dict],
) -> None:
    conn.execute(
        "INSERT INTO invoices_processed (run_id, invoice_number, vendor, content_hash, total, "
        "currency, status, reasons, extras, invoice_json, source_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            inv.invoice_number,
            inv.vendor,
            content_hash(inv),
            inv.total,
            inv.currency,
            status.value,
            json.dumps(reasons),
            json.dumps(inv.extras),
            inv.model_dump_json(),
            inv.source_file,
        ),
    )
    conn.commit()
