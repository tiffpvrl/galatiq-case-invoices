"""Human review queue: resolve invoices the system escalated.

Every decision is stored with the reviewer and a note. That record is what a
production version would learn thresholds from (e.g. which flags reviewers
always approve).
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from .ledger import content_hash
from .models import Invoice, Outcome
from .payment import PaymentResult, mock_payment, pay
from .routing import suggest_owner

DECISIONS = ("approve", "reject", "hold")


@dataclass
class PendingReview:
    ledger_id: int
    invoice_number: str | None
    vendor: str | None
    total: float | None
    source_file: str | None
    reasons: list[dict]
    created_at: str
    owner: str = "AP clerk"
    owner_why: list[str] = field(default_factory=list)


class ReviewError(Exception):
    pass


def pending(conn: sqlite3.Connection) -> list[PendingReview]:
    """Escalated invoices with no approve/reject decision yet (a hold stays pending)."""
    import json

    rows = conn.execute(
        "SELECT p.* FROM invoices_processed p WHERE p.status = ? AND NOT EXISTS "
        "(SELECT 1 FROM review_decisions d WHERE d.ledger_id = p.id "
        " AND d.decision IN ('approve', 'reject')) ORDER BY p.id",
        (Outcome.NEEDS_HUMAN_REVIEW.value,),
    )
    queue = []
    for r in rows:
        reasons = json.loads(r["reasons"])
        owner, why = suggest_owner(reasons, r["total"])
        queue.append(PendingReview(r["id"], r["invoice_number"], r["vendor"], r["total"],
                                   r["source_file"], reasons, r["created_at"], owner, why))
    return queue


def amount_due(conn: sqlite3.Connection, inv: Invoice, ledger_id: int) -> float:
    """What is still owed: the total minus anything already paid under the same
    invoice number. Approving INV-1004's revision pays only the difference."""
    paid = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM invoices_processed "
        "WHERE invoice_number = ? AND status = ? AND id != ?",
        (inv.invoice_number, Outcome.PAID.value, ledger_id),
    ).fetchone()[0]
    return round((inv.total or 0) - paid, 2)


def resolve(
    conn: sqlite3.Connection,
    ledger_id: int,
    decision: str,
    reviewer: str,
    note: str = "",
    gateway: Callable[[str, float], dict] = mock_payment,
) -> PaymentResult | None:
    """Record a human decision. Approving pays (idempotently); rejecting closes
    the invoice; holding leaves it pending. Returns the payment, if one was made."""
    decision = decision.lower()
    if decision not in DECISIONS:
        raise ReviewError(f"decision must be one of {', '.join(DECISIONS)}")
    row = conn.execute("SELECT * FROM invoices_processed WHERE id = ?", (ledger_id,)).fetchone()
    if row is None:
        raise ReviewError(f"no ledger entry {ledger_id}")
    if row["status"] != Outcome.NEEDS_HUMAN_REVIEW.value:
        raise ReviewError(f"entry {ledger_id} is {row['status']}, not awaiting review")

    payment = None
    new_status = None
    if decision == "approve":
        inv = Invoice.model_validate_json(row["invoice_json"])
        if not inv.vendor or inv.total is None or inv.total <= 0 or not inv.line_items:
            raise ReviewError("cannot pay: the invoice is incomplete (extraction failed or "
                              "missing vendor/total). Reject it or fix the source.")
        due = amount_due(conn, inv, ledger_id)
        if due > 0:
            key = f"{inv.invoice_number}:{content_hash(inv)}"
            payment = pay(conn, key, inv.vendor, due, gateway)
            if payment.status != "success":
                raise ReviewError(f"payment failed, invoice left pending: {payment.detail}")
        new_status = Outcome.PAID
    elif decision == "reject":
        new_status = Outcome.REJECTED

    if new_status:
        conn.execute("UPDATE invoices_processed SET status = ? WHERE id = ?",
                     (new_status.value, ledger_id))
    conn.execute(
        "INSERT INTO review_decisions (ledger_id, decision, reviewer, note) VALUES (?, ?, ?, ?)",
        (ledger_id, decision, reviewer, note),
    )
    conn.commit()
    return payment
