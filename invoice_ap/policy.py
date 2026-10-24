"""The policy floor: deterministic rules that no LLM output can override.

The model may make a decision more cautious, never less. The floor is computed
first; if it already escalates or rejects, the approver is not called at all
(it could not change the outcome, so the call would only cost money).
"""

from dataclasses import dataclass, field

from .config import SETTINGS, Settings
from .models import Decision, Finding, Invoice, Severity

# Flags that mean identity, authorization or ability-to-settle is in doubt.
# These always go to a human. Everything else is evidence for the approver.
FORCE_ESCALATE = {
    "PRESSURE_LANGUAGE": "Urgency language is meant to bypass review",
    "IMMEDIATE_TERMS": "Demand for immediate payment leaves no review window",
    "RELATIVE_DUE_DATE": "A relative due date ('yesterday') is a pressure/fraud signal",
    "DUE_BEFORE_ISSUE": "Due date precedes the invoice date",
    "UNKNOWN_VENDOR": "Vendor is not in the approved vendor master",
    "VENDOR_RENAMED": "Vendor master must be updated before paying a renamed vendor",
    "INVOICE_NUMBER_COLLISION": "Invoice number already used by a different vendor",
    "POSSIBLE_REBILL": "Same bill already processed under another number: duplicate-payment risk",
    "REVISION_OF_PRIOR": "Revisions need human confirmation against the original",
    "NON_SETTLEABLE_CURRENCY": "Treasury must book an FX rate",
}

# The subset of FORCE_ESCALATE that points at deception or pressure rather than a
# paperwork question. A rejected invoice carrying one of these is grouped as
# "possible fraud" in the report, and no vendor draft is written for it.
FRAUD_SIGNALS = {
    "PRESSURE_LANGUAGE", "IMMEDIATE_TERMS", "RELATIVE_DUE_DATE", "DUE_BEFORE_ISSUE",
    "UNKNOWN_VENDOR", "INVOICE_NUMBER_COLLISION", "POSSIBLE_REBILL",
}

_CAUTION = {Decision.APPROVE: 0, Decision.ESCALATE: 1, Decision.REJECT: 2}


def most_cautious(*decisions: Decision) -> Decision:
    return max(decisions, key=_CAUTION.__getitem__)


@dataclass
class Floor:
    decision: Decision
    triggers: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)  # extra findings the floor adds


def rule_floor(inv: Invoice, findings: list[Finding], settings: Settings = SETTINGS) -> Floor:
    if any(f.severity == Severity.CRITICAL for f in findings):
        codes = sorted({f.code for f in findings if f.severity == Severity.CRITICAL})
        return Floor(Decision.REJECT, [f"CRITICAL_FINDINGS[{','.join(codes)}]"])

    triggers: list[str] = []
    extra: list[Finding] = []
    for f in findings:
        if f.code in FORCE_ESCALATE and f.code not in triggers:
            triggers.append(f.code)
    if inv.total is not None and inv.total > settings.policy.cfo_threshold:
        triggers.append("OVER_CFO_THRESHOLD")
        extra.append(Finding(
            code="OVER_CFO_THRESHOLD", severity=Severity.WARNING,
            message=f"Total ${inv.total:,.2f} exceeds ${settings.policy.cfo_threshold:,.2f}; "
                    "needs CFO-level sign-off.",
        ))
    return Floor(Decision.ESCALATE if triggers else Decision.APPROVE, triggers, extra)


def needs_reasoning(inv: Invoice, findings: list[Finding], settings: Settings = SETTINGS) -> bool:
    """Clean invoices below the scrutiny threshold are approved by code: with
    no flags there is nothing to weigh, and an LLM would only add cost and
    non-determinism."""
    if any(f.severity == Severity.WARNING for f in findings):
        return True
    return inv.total is not None and inv.total >= settings.policy.scrutiny_threshold
