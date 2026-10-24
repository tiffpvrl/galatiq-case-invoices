"""Typed objects passed between pipeline steps."""

from __future__ import annotations

import datetime as dt
from enum import Enum

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    item: str
    quantity: float
    unit_price: float | None = None
    amount: float | None = None
    note: str | None = None


class Invoice(BaseModel):
    """Extracted invoice. Optional fields are None when absent from the source;
    an absent field is never filled in by guessing."""

    invoice_number: str | None = None
    vendor: str | None = None
    date: dt.date | None = None
    due_date: dt.date | None = None
    # Raw due-date text kept when it was relative ("yesterday") or unparseable.
    due_date_text: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    shipping: float | None = None
    total: float | None = None
    currency: str = "USD"
    payment_terms: str | None = None
    notes: str | None = None
    revision: str | None = None
    # Anything else the source carried. Never blocks processing.
    extras: dict = Field(default_factory=dict)
    source_file: str | None = None
    source_text: str | None = Field(default=None, exclude=True)


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    # Whether the vendor/human can plausibly fix it (stock shortfall, unknown
    # item) as opposed to a fundamental defect (negative total, duplicate).
    disputable: bool = False
    item: str | None = None


class Decision(str, Enum):
    APPROVE = "APPROVE"
    ESCALATE = "ESCALATE"
    REJECT = "REJECT"


class Outcome(str, Enum):
    """Every invoice ends in exactly one of these."""

    PAID = "PAID"
    REJECTED = "REJECTED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
