"""SQLite schema and seed data for the simulated systems.

The inventory table stands in for the PO and goods receipt: goods that have
been ordered, delivered and are on hand. The first four rows are the case
README's starter data and must not change; extra SKUs only add test coverage.
"""

import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item       TEXT PRIMARY KEY,
    stock      INTEGER NOT NULL CHECK (stock >= 0),
    unit_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
    name     TEXT PRIMARY KEY,
    approved INTEGER NOT NULL DEFAULT 1
);

-- A former or alternate name that maps to an approved vendor.
CREATE TABLE IF NOT EXISTS vendor_aliases (
    alias       TEXT PRIMARY KEY,
    vendor_name TEXT NOT NULL REFERENCES vendors(name),
    kind        TEXT NOT NULL DEFAULT 'former_name'
);

CREATE TABLE IF NOT EXISTS invoices_processed (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    invoice_number TEXT,
    vendor         TEXT,
    content_hash   TEXT NOT NULL,
    total          REAL,
    currency       TEXT,
    status         TEXT NOT NULL,
    reasons        TEXT NOT NULL DEFAULT '[]',
    extras         TEXT NOT NULL DEFAULT '{}',
    invoice_json   TEXT,
    source_file    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_processed_number ON invoices_processed(invoice_number);
CREATE INDEX IF NOT EXISTS idx_processed_hash   ON invoices_processed(content_hash);

-- A human's decision on an invoice the system escalated.
CREATE TABLE IF NOT EXISTS review_decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id  INTEGER NOT NULL REFERENCES invoices_processed(id),
    decision   TEXT NOT NULL,
    reviewer   TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Idempotency: one payment per key, so a re-run cannot double-pay.
CREATE TABLE IF NOT EXISTS payments (
    idempotency_key TEXT PRIMARY KEY,
    vendor          TEXT NOT NULL,
    amount          REAL NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# (item, stock, unit_price)
INVENTORY_SEED = [
    # README starter rows, unchanged.
    ("WidgetA", 15, 250.00),
    ("WidgetB", 10, 500.00),
    ("GadgetX", 5, 750.00),
    ("FakeItem", 0, 0.00),
    # Extra SKUs so a clean invoice over the $10K scrutiny threshold is
    # possible (the starter stock is too small to reach it without
    # using every unit).
    ("SteelBracket", 400, 45.00),
    ("HydraulicPump", 20, 1_200.00),
    ("ControlPanel", 10, 2_500.00),
]

VENDOR_SEED = [
    "Widgets Inc.",
    "Gadgets Co.",
    "Precision Parts Ltd.",
    "Global Supply Chain Partners",
    "Acme Industrial Supplies",
    "MegaWidgets Corp",
    "Summit Manufacturing Co.",
    "Consolidated Materials Group",
    "Atlas Industrial Supply",
    "TechParts International",
    "Reliable Components Inc.",
    # Registered under its former name; INV-1012 bills as "QuickShip
    # Distributers (formerly FastShip Ltd.)".
    "FastShip Ltd.",
]

# (alias, vendor_name)
ALIAS_SEED = [
    ("QuickShip Distributers", "FastShip Ltd."),
]


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str = DB_PATH, reset: bool = False) -> sqlite3.Connection:
    """Create the schema and seed it. With reset=True, rebuild from scratch."""
    if reset and str(path) != ":memory:":
        Path(path).unlink(missing_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO inventory (item, stock, unit_price) VALUES (?, ?, ?)",
        INVENTORY_SEED,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO vendors (name, approved) VALUES (?, 1)",
        [(v,) for v in VENDOR_SEED],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO vendor_aliases (alias, vendor_name) VALUES (?, ?)",
        ALIAS_SEED,
    )
    conn.commit()
    return conn


if __name__ == "__main__":
    conn = init_db(reset=True)
    rows = conn.execute("SELECT item, stock, unit_price FROM inventory").fetchall()
    print(f"Seeded {DB_PATH} with {len(rows)} inventory items:")
    for r in rows:
        print(f"  {r['item']:<14} stock={r['stock']:<4} price=${r['unit_price']:,.2f}")
