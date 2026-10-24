"""Read-only lookups against the simulated systems.

Validation uses these directly, and the approver agent is given the same
functions as tools, so it can verify claims instead of trusting a summary.
"""

import datetime as dt
import difflib
import json
import sqlite3

from .config import SETTINGS
from .normalize import item_key, vendor_key


def get_item(conn: sqlite3.Connection, name: str) -> dict | None:
    """Catalog entry for an item name, matched on case/whitespace/punctuation
    only. Returns None when the item is not in the catalog."""
    key = item_key(name)
    for row in conn.execute("SELECT item, stock, unit_price FROM inventory"):
        if item_key(row["item"]) == key:
            return dict(row)
    return None


def suggest_items(conn: sqlite3.Connection, name: str) -> list[str]:
    """'Did you mean' hints for a human. Never used to substitute an item."""
    catalog = {item_key(r["item"]): r["item"] for r in conn.execute("SELECT item FROM inventory")}
    close = difflib.get_close_matches(item_key(name), list(catalog), n=2, cutoff=0.7)
    return [catalog[k] for k in close]


def get_vendor(conn: sqlite3.Connection, name: str | None) -> dict:
    """Vendor master status.

    status: 'approved' (name matches), 'renamed' (matches a former/alternate
    name of an approved vendor), 'unapproved' (listed but not approved), or
    'unknown'. 'canonical' is the master record's name when one matched.
    """
    if not name:
        return {"status": "unknown", "canonical": None}
    key = vendor_key(name)
    for row in conn.execute("SELECT name, approved FROM vendors"):
        if vendor_key(row["name"]) == key:
            status = "approved" if row["approved"] else "unapproved"
            return {"status": status, "canonical": row["name"]}
    for row in conn.execute("SELECT alias, vendor_name FROM vendor_aliases"):
        if vendor_key(row["alias"]) == key:
            return {"status": "renamed", "canonical": row["vendor_name"]}
    return {"status": "unknown", "canonical": None}


def get_invoice_history(conn: sqlite3.Connection, invoice_number: str | None) -> list[dict]:
    """Prior ledger records for an invoice number, oldest first."""
    if not invoice_number:
        return []
    rows = conn.execute(
        "SELECT id, invoice_number, vendor, content_hash, total, currency, status, source_file, "
        "created_at FROM invoices_processed WHERE invoice_number = ? ORDER BY id",
        (invoice_number,),
    )
    return [dict(r) for r in rows]


def get_vendor_history(
    conn: sqlite3.Connection, vendor: str, days: int = 30, as_of: dt.date | None = None
) -> dict:
    """Recent ledger invoices from a vendor, newest first. Lets the approver test
    split billing: sibling invoices whose sum crosses an approval threshold."""
    as_of = as_of or SETTINGS.as_of_date
    cutoff = as_of - dt.timedelta(days=days)
    recent = []
    for row in conn.execute("SELECT invoice_number, vendor, total, status, invoice_json "
                            "FROM invoices_processed ORDER BY id DESC"):
        if vendor_key(row["vendor"] or "") != vendor_key(vendor):
            continue
        invoice_date = None
        if row["invoice_json"]:
            raw = json.loads(row["invoice_json"]).get("date")
            invoice_date = dt.date.fromisoformat(raw) if raw else None
        if invoice_date is None or invoice_date >= cutoff:
            recent.append({"invoice_number": row["invoice_number"],
                           "invoice_date": str(invoice_date) if invoice_date else None,
                           "total": row["total"], "status": row["status"]})
    return {"vendor": vendor, "window_days": days, "as_of": str(as_of),
            "count": len(recent), "combined_total": round(sum(r["total"] or 0 for r in recent), 2),
            "invoices": recent[:20]}


def committed_quantities(conn: sqlite3.Connection) -> dict[str, float]:
    """Quantity per item already claimed by paid invoices in the ledger, keyed
    by item_key. Used only when settings.reserve_stock is on."""
    claimed: dict[str, float] = {}
    rows = conn.execute("SELECT invoice_json FROM invoices_processed WHERE status = 'PAID'")
    for row in rows:
        if not row["invoice_json"]:
            continue
        for line in json.loads(row["invoice_json"]).get("line_items", []):
            key = item_key(line["item"])
            claimed[key] = claimed.get(key, 0) + (line.get("quantity") or 0)
    return claimed
