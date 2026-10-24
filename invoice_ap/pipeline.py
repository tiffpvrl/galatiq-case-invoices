"""The end-to-end flow for one invoice file.

ingest -> validate -> policy floor -> (auto-approve | approver/critic) -> pay
or reject or escalate -> ledger -> notification.

Invariants: an invoice never reaches payment past a CRITICAL finding or a
forced-escalation flag, and any error on the way ends in human review, never
in a payment.
"""

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .approval import ApprovalResult, run_approval
from .config import SETTINGS, Settings
from .extract import ExtractionFailed, IngestResult, ingest
from .ledger import content_hash, record
from .llm import Provider, Usage
from .models import Decision, Finding, Invoice, Outcome, Severity
from .notify import Notification, write_notification
from .payment import PaymentResult, mock_payment, pay
from .policy import needs_reasoning, most_cautious, rule_floor
from .trace import Trace
from .validate import hard_fails, validate


@dataclass
class Result:
    file: str
    outcome: Outcome
    invoice: Invoice
    findings: list[Finding] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # operational problems (e.g. LLM outage)
    path: list[str] = field(default_factory=list)  # human-readable decision path
    rationale: str = ""
    method: str = ""
    approval: ApprovalResult | None = None
    payment: PaymentResult | None = None
    notification: Notification | None = None
    usage: list[Usage] = field(default_factory=list)
    duration_ms: float = 0.0
    trace: list[dict] = field(default_factory=list)

    @property
    def llm_calls(self) -> int:
        return len(self.usage)

    def to_dict(self) -> dict:
        inv = self.invoice
        return {
            "file": self.file,
            "outcome": self.outcome.value,
            "invoice_number": inv.invoice_number,
            "vendor": inv.vendor,
            "total": inv.total,
            "currency": inv.currency,
            "extraction": self.method,
            "decision_path": self.path,
            "rationale": self.rationale,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "payment": self.payment.reference if self.payment else None,
            "llm_calls": self.llm_calls,
            "input_tokens": sum(u.input_tokens for u in self.usage),
            "output_tokens": sum(u.output_tokens for u in self.usage),
            "duration_ms": self.duration_ms,
            "notification": self.notification.model_dump() if self.notification else None,
        }


def _reasons(findings: list[Finding]) -> list[dict]:
    return [f.model_dump(mode="json") for f in findings if f.severity != Severity.INFO]


def process_file(
    path: str | Path,
    conn: sqlite3.Connection,
    provider: Provider,
    settings: Settings = SETTINGS,
    run_id: str | None = None,
    out_dir: Path | None = None,
    gateway: Callable[[str, float], dict] = mock_payment,
) -> Result:
    import time

    path = Path(path)
    run_id = run_id or uuid.uuid4().hex[:8]
    trace = Trace(run_id, path.name, out_dir / "trace.jsonl" if out_dir else None)
    usage_start = len(provider.usage)
    started = time.perf_counter()

    inv = Invoice(source_file=path.name)
    findings: list[Finding] = []
    result = Result(file=path.name, outcome=Outcome.NEEDS_HUMAN_REVIEW, invoice=inv)

    def finish(outcome: Outcome, rationale: str = "") -> Result:
        result.outcome, result.rationale = outcome, rationale
        result.reasons = _reasons(result.findings)
        try:
            record(conn, run_id, result.invoice, outcome, result.reasons)
        except Exception as exc:  # ledger failure must not hide the outcome
            trace.event("ledger_error", error=str(exc))
        facts = {
            "outcome": outcome.value, "invoice_number": result.invoice.invoice_number,
            "vendor": result.invoice.vendor, "total": result.invoice.total,
            "source_file": path.name, "rationale": rationale, "reasons": result.reasons,
            "payment": result.payment.reference if result.payment else None,
        }
        with trace.stage("notify"):
            result.notification = write_notification(facts, provider)
        if out_dir:
            note_dir = out_dir / "notifications"
            note_dir.mkdir(parents=True, exist_ok=True)
            n = result.notification
            (note_dir / f"{path.stem}.txt").write_text(f"Subject: {n.subject}\n\n{n.body}\n")
        result.usage = provider.usage[usage_start:]
        result.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        trace.event("done", outcome=outcome.value, llm_calls=result.llm_calls,
                    duration_ms=result.duration_ms)
        result.trace = trace.events
        return result

    try:
        # 1. Ingest
        try:
            with trace.stage("ingest"):
                ingested: IngestResult = ingest(path, provider, settings)
        except ExtractionFailed as exc:
            result.findings = [Finding(code="EXTRACTION_FAILED", severity=Severity.WARNING,
                                       message=f"Could not extract the invoice: {exc}")]
            result.path.append("ingest:failed")
            return finish(Outcome.NEEDS_HUMAN_REVIEW, "Extraction failed; a human must read it.")
        inv = result.invoice = ingested.invoice
        result.method = ingested.method
        result.path.append(f"ingest:{ingested.method}")
        result.warnings += ingested.notes

        # 2. Validate (duplicates, completeness, arithmetic, stock, price, vendor, dates, fraud)
        with trace.stage("validate"):
            result.findings = findings = validate(inv, conn, settings)
        crit = hard_fails(findings)
        result.path.append(f"validate:{len(crit)} hard fail, "
                           f"{sum(f.severity == Severity.WARNING for f in findings)} flag")
        if crit:
            codes = ", ".join(sorted({f.code for f in crit}))
            return finish(Outcome.REJECTED, f"Hard validation failure: {codes}.")

        # 3. Policy floor (deterministic; the LLM can only be more cautious)
        floor = rule_floor(inv, findings, settings)
        result.findings = findings = findings + floor.findings
        result.path.append(f"floor:{floor.decision.value}")
        if floor.decision == Decision.REJECT:
            return finish(Outcome.REJECTED, "; ".join(floor.triggers))
        if floor.decision == Decision.ESCALATE:
            return finish(Outcome.NEEDS_HUMAN_REVIEW,
                          "Policy requires a human: " + ", ".join(floor.triggers) + ".")

        # 4. Approval
        if needs_reasoning(inv, findings, settings):
            with trace.stage("approval"):
                approval = result.approval = run_approval(inv, findings, conn, provider, settings)
            decision = most_cautious(floor.decision, approval.decision)
            result.path.append(f"approver:{decision.value} ({approval.rounds} critic round(s))")
            if approval.llm_error:
                result.warnings.append(f"approval model unavailable ({approval.llm_error})")
            rationale = approval.rationale
        else:
            decision = Decision.APPROVE
            rationale = "No findings and below the scrutiny threshold; approved by policy."
            result.path.append("auto-approve")

        if decision == Decision.REJECT:
            return finish(Outcome.REJECTED, rationale)
        if decision == Decision.ESCALATE:
            return finish(Outcome.NEEDS_HUMAN_REVIEW, rationale)

        # 5. Payment (idempotent)
        key = f"{inv.invoice_number}:{content_hash(inv)}"
        with trace.stage("payment"):
            result.payment = pay(conn, key, inv.vendor, inv.total, gateway)
        if result.payment.status != "success":
            result.findings.append(Finding(code="PAYMENT_FAILED", severity=Severity.WARNING,
                                           message=result.payment.detail))
            result.path.append("pay:failed")
            return finish(Outcome.NEEDS_HUMAN_REVIEW, "Payment failed; nothing was paid.")
        result.path.append("pay")
        return finish(Outcome.PAID, rationale)

    except Exception as exc:  # never pay on an unexpected error
        trace.event("internal_error", error=f"{type(exc).__name__}: {exc}")
        result.findings.append(Finding(code="INTERNAL_ERROR", severity=Severity.WARNING,
                                       message=f"{type(exc).__name__}: {exc}"))
        result.path.append("error")
        return finish(Outcome.NEEDS_HUMAN_REVIEW, "Unexpected error; nothing was paid.")
