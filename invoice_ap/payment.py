"""Mock payment API with an idempotency guard."""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("invoice_ap.payment")


def mock_payment(vendor, amount):
    """The case's mock banking API."""
    log.info("Paid %s to %s", amount, vendor)
    return {"status": "success"}


@dataclass
class PaymentResult:
    status: str  # "success" | "failed"
    reference: str | None = None
    detail: str = ""
    replayed: bool = False


def pay(
    conn: sqlite3.Connection,
    key: str,
    vendor: str,
    amount: float,
    gateway: Callable[[str, float], dict] = mock_payment,
) -> PaymentResult:
    """Pay once per idempotency key. Re-running the same invoice returns the
    original result without calling the gateway again."""
    existing = conn.execute(
        "SELECT idempotency_key FROM payments WHERE idempotency_key = ?", (key,)
    ).fetchone()
    if existing:
        return PaymentResult("success", reference=key, detail="already paid", replayed=True)
    try:
        response = gateway(vendor, amount)
    except Exception as exc:
        return PaymentResult("failed", detail=f"gateway error: {exc}")
    if response.get("status") != "success":
        return PaymentResult("failed", detail=f"gateway declined: {response}")
    conn.execute(
        "INSERT INTO payments (idempotency_key, vendor, amount) VALUES (?, ?, ?)",
        (key, vendor, amount),
    )
    conn.commit()
    return PaymentResult("success", reference=key)
