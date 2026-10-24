"""Learning from human review decisions.

Every escalation a person resolves is stored with their decision. This module
turns those decisions into *suggested* policy changes, per finding code:

  - if reviewers almost always approve a flag we force-escalate, the rule may be
    costing review time for no benefit (suggest letting the approver weigh it)
  - if reviewers almost always reject a flag we only warn about, it may deserve
    to be a hard fail
  - otherwise the flag needs human judgment and should stay as it is

Two safeguards against over-reading small data:
  - Only single-flag invoices count. When an invoice carried three flags, the
    decision says nothing about which one the reviewer cared about.
  - A suggestion needs the lower bound of a 95% Wilson interval on the approve
    (or reject) rate to clear a bar, so "5 of 5" does not move policy but
    "12 of 12" does.

Suggestions are never applied automatically. A person changes the config, as
with any control change.
"""

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from .policy import FORCE_ESCALATE

DEFAULT_BAR = 0.70  # lower confidence bound the rate must clear


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    """Lower end of the 95% Wilson score interval for a proportion."""
    if n == 0:
        return 0.0
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / (1 + z * z / n))


@dataclass
class CodeStats:
    code: str
    handling: str  # "forced" (policy always escalates) | "approver" (model judgment)
    approved: int = 0  # all decisions on invoices carrying this code
    rejected: int = 0
    held: int = 0
    solo_approved: int = 0  # decisions where this was the only flag
    solo_rejected: int = 0
    approve_lower: float = 0.0
    reject_lower: float = 0.0
    suggestion: str | None = None
    verdict: str = "keep"  # keep | relax | harden | too few (decisions)

    @property
    def solo_n(self) -> int:
        return self.solo_approved + self.solo_rejected

    @property
    def total_n(self) -> int:
        return self.approved + self.rejected


@dataclass
class InsightsReport:
    decisions: int = 0
    holds: int = 0
    reviewers: dict = field(default_factory=dict)
    simulated: bool = False
    bar: float = DEFAULT_BAR
    codes: list[CodeStats] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def compute_insights(conn: sqlite3.Connection, bar: float = DEFAULT_BAR) -> InsightsReport:
    rows = conn.execute(
        "SELECT d.decision, d.reviewer, p.reasons FROM review_decisions d "
        "JOIN invoices_processed p ON p.id = d.ledger_id ORDER BY d.id"
    ).fetchall()

    report = InsightsReport(bar=bar)
    stats: dict[str, CodeStats] = {}
    reviewers: dict[str, int] = defaultdict(int)
    for row in rows:
        reviewers[row["reviewer"]] += 1
        decision = row["decision"]
        if decision == "hold":
            report.holds += 1
        codes = sorted({r["code"] for r in json.loads(row["reasons"]) if r["severity"] != "INFO"})
        for code in codes:
            s = stats.setdefault(code, CodeStats(
                code, "forced" if code in FORCE_ESCALATE else "approver"))
            if decision == "approve":
                s.approved += 1
                s.solo_approved += len(codes) == 1
            elif decision == "reject":
                s.rejected += 1
                s.solo_rejected += len(codes) == 1
            else:
                s.held += 1
        if decision in ("approve", "reject"):
            report.decisions += 1

    report.reviewers = dict(reviewers)
    report.simulated = any(name.lower().startswith("sim") for name in reviewers)
    for s in stats.values():
        _assess(s, bar)
    report.codes = sorted(stats.values(), key=lambda s: (-s.total_n, s.code))
    return report


def _assess(s: CodeStats, bar: float) -> None:
    n = s.solo_n
    s.approve_lower = round(wilson_lower(s.solo_approved, n), 3)
    s.reject_lower = round(wilson_lower(s.solo_rejected, n), 3)
    if s.approve_lower >= bar:
        s.verdict = "relax"
        if s.handling == "forced":
            s.suggestion = (
                f"Reviewers approved {s.solo_approved} of {n} single-flag {s.code} invoices "
                f"(at least {s.approve_lower:.0%} at 95% confidence). Consider removing {s.code} "
                "from FORCE_ESCALATE in invoice_ap/policy.py so the approver can weigh it.")
        else:
            s.suggestion = (
                f"Reviewers approved {s.solo_approved} of {n} single-flag {s.code} invoices the "
                "approver escalated. The approver may be too cautious here: review its prompt or "
                "add an explicit rule for this flag.")
    elif s.reject_lower >= bar:
        s.verdict = "harden"
        s.suggestion = (
            f"Reviewers rejected {s.solo_rejected} of {n} single-flag {s.code} invoices "
            f"(at least {s.reject_lower:.0%} at 95% confidence). Consider making {s.code} a "
            "hard fail (CRITICAL) in invoice_ap/validate.py.")
    elif n < 5 or max(s.approve_lower, s.reject_lower) == 0:
        s.verdict = "too few"
    else:
        s.verdict = "keep"


def render(console, report: InsightsReport) -> None:
    from rich.markup import escape
    from rich.table import Table

    if not report.decisions and not report.holds:
        console.print("[yellow]No review decisions recorded yet.[/] Resolve escalated invoices "
                      "with `python main.py --review`, then run this again.")
        return
    if report.simulated:
        console.print("[bold yellow]These decisions are from SIMULATED reviewers. They show how "
                      "the report works, not how real reviewers behave.[/]\n")
    names = ", ".join(f"{k} ({v})" for k, v in report.reviewers.items())
    console.print(f"[bold]{report.decisions} approve/reject decisions[/] "
                  f"({report.holds} hold(s)) by {escape(names)}\n")

    table = Table(title="Review outcomes by flag (suggestions use single-flag invoices only)")
    table.add_column("Flag", no_wrap=True)
    for col in ("Handled", "Appr.", "Rej.", "Solo", "Verdict"):
        table.add_column(col, no_wrap=True)
    colors = {"relax": "green", "harden": "red", "keep": "white", "too few": "dim"}
    for s in report.codes:
        table.add_row(s.code, s.handling, str(s.approved), str(s.rejected), str(s.solo_n),
                      f"[{colors[s.verdict]}]{s.verdict}[/]")
    console.print(table)

    suggestions = [s for s in report.codes if s.suggestion]
    if suggestions:
        console.print("\n[bold]Suggested changes[/] (never applied automatically)")
        for s in suggestions:
            console.print(f"  • {escape(s.suggestion)}")
    else:
        console.print(f"\nNo changes suggested: no flag has a single-flag approve or reject rate "
                      f"whose 95% lower bound clears {report.bar:.0%}.")
