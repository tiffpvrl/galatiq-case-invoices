"""All policy thresholds and runtime settings in one place.

Thresholds are policy parameters, not facts: see docs/process-notes.md for the
rationale and the evidence (and its limits) behind each one.
"""

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INVOICE_DIR = DATA_DIR / "invoices"
DB_PATH = DATA_DIR / "inventory.db"
RUN_DIR = REPO_ROOT / "runs"


@dataclass(frozen=True)
class Policy:
    # Totals at or above this need the approver/critic loop even when clean.
    scrutiny_threshold: float = 10_000.00
    # Totals above this go to a CFO tier. Escalate, never reject: rejecting a
    # legitimate large invoice only delays it.
    cfo_threshold: float = 50_000.00
    # Totals within this fraction below a threshold get a near-threshold flag
    # (possible split billing to stay under a limit).
    near_threshold_band: float = 0.05
    # Price above catalog list: flag over this fraction, with a dollar cap per
    # unit so a percentage alone cannot excuse a large absolute overcharge.
    price_above_tolerance: float = 0.02
    price_above_cap_per_unit: float = 25.00
    # Price below catalog list: flag only past this fraction (overpaying is the
    # loss; smaller discounts are INFO).
    price_below_flag: float = 0.10
    # Allowed difference, in currency units, when recomputing totals.
    arithmetic_tolerance: float = 0.01
    # Stated payment terms vs. the actual date span, in days.
    terms_mismatch_days: int = 3
    require_known_vendor: bool = True
    settleable_currencies: tuple[str, ...] = ("USD",)
    # Phrases that signal manufactured urgency, scanned in notes, terms and
    # the source text. A WARNING: pressure is meant to bypass review.
    pressure_phrases: tuple[str, ...] = (
        "urgent",
        "pay immediately",
        "immediate payment",
        "wire transfer",
        "avoid penalties",
        "act now",
        "final notice",
        # Authority claims and prompt-injection attempts. The floor escalates
        # on these in code, before any model sees the invoice.
        "ignore previous instructions",
        "ignore all previous",
        "pre-approved",
        "already approved",
        "authorized by",
        "without review",
        "skip review",
        "do not verify",
    )


@dataclass(frozen=True)
class Settings:
    # The fixed "today" for past-due checks, so runs are reproducible. The
    # sample invoices are dated January 2026; the wall clock would flag nearly
    # all of them.
    as_of_date: date = field(
        default_factory=lambda: date.fromisoformat(
            os.environ.get("AP_AS_OF_DATE", "2026-02-01")
        )
    )
    max_extraction_retries: int = 3
    max_critique_rounds: int = 2
    # When True, stock already claimed by earlier approvals in the same batch
    # is excluded (a ledger lookup; inventory itself is never mutated).
    reserve_stock: bool = False
    policy: Policy = field(default_factory=Policy)


SETTINGS = Settings()
