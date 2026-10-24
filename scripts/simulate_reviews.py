"""Fill a SEPARATE demo database with simulated review decisions.

Usage:
  python scripts/simulate_reviews.py                 # writes data/demo_reviews.db
  python main.py --insights --db data/demo_reviews.db

Real decisions only exist once people use `main.py --review`. This script makes
synthetic ledger rows and decisions, attributed to the reviewer "simulated", so
the insights report can be demonstrated. The report labels such data clearly.
Never point it at your real database.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ap.config import DATA_DIR
from invoice_ap.db import init_db

# (finding code, approvals, rejections, extra single-flag? yes). Rates are invented
# to show each verdict: relax, harden, keep, and too little data.
SCENARIOS = [
    ("NON_SETTLEABLE_CURRENCY", 13, 0, "EUR invoices: treasury books the FX and approves"),
    ("VENDOR_RENAMED", 12, 1, "renames confirmed against the vendor master"),
    ("PRESSURE_LANGUAGE", 0, 11, "urgency language rejected as fraud"),
    ("UNKNOWN_VENDOR", 6, 6, "needs a real judgment each time"),
    ("REVISION_OF_PRIOR", 3, 1, "too few decisions to say"),
]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DATA_DIR / "demo_reviews.db"))
    args = p.parse_args()

    conn = init_db(args.db, reset=True)
    n = 0
    for code, approvals, rejections, _ in SCENARIOS:
        for decision, count in (("approve", approvals), ("reject", rejections)):
            for _ in range(count):
                n += 1
                reasons = [{"code": code, "severity": "WARNING", "message": "simulated",
                            "disputable": False}]
                cur = conn.execute(
                    "INSERT INTO invoices_processed (run_id, invoice_number, vendor, content_hash, "
                    "total, currency, status, reasons, source_file) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("sim", f"SIM-{n:04d}", "Simulated Vendor", f"sim{n}", 1000.0, "USD",
                     "PAID" if decision == "approve" else "REJECTED", json.dumps(reasons), "simulated"))
                conn.execute("INSERT INTO review_decisions (ledger_id, decision, reviewer, note) "
                             "VALUES (?,?,?,?)", (cur.lastrowid, decision, "simulated", "synthetic"))
    # A multi-flag invoice, to show that it does not count toward single-flag stats.
    reasons = [{"code": c, "severity": "WARNING", "message": "simulated", "disputable": False}
               for c in ("VENDOR_RENAMED", "NEAR_THRESHOLD")]
    cur = conn.execute(
        "INSERT INTO invoices_processed (run_id, invoice_number, vendor, content_hash, total, "
        "currency, status, reasons, source_file) VALUES ('sim','SIM-MULTI','Simulated Vendor',"
        "'simmulti',9900,'USD','REJECTED',?, 'simulated')", (json.dumps(reasons),))
    conn.execute("INSERT INTO review_decisions (ledger_id, decision, reviewer, note) "
                 "VALUES (?, 'reject', 'simulated', 'synthetic')", (cur.lastrowid,))
    conn.commit()
    print(f"wrote {n + 1} simulated decisions to {args.db}")


if __name__ == "__main__":
    main()
