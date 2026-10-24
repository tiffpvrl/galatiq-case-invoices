"""Console output and run summary metrics."""

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .llm import estimated_cost
from .models import Outcome
from .pipeline import Result

STYLE = {
    Outcome.PAID: ("green", "PAID"),
    Outcome.REJECTED: ("red", "REJECTED"),
    Outcome.NEEDS_HUMAN_REVIEW: ("yellow", "NEEDS REVIEW"),
}
SEVERITY_STYLE = {"CRITICAL": "red", "WARNING": "yellow", "INFO": "dim"}


def money(value) -> str:
    if value is None:
        return "n/a"
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def summarize(results: list[Result], provider_name: str = "") -> dict:
    n = len(results)
    by = {o: [r for r in results if r.outcome == o] for o in Outcome}
    usage = [u for r in results for u in r.usage]
    total = lambda rs: round(sum(max(r.invoice.total or 0, 0) for r in rs), 2)
    cost = estimated_cost(usage)
    return {
        "provider": provider_name,
        "invoices": n,
        "paid": len(by[Outcome.PAID]),
        "rejected": len(by[Outcome.REJECTED]),
        "needs_review": len(by[Outcome.NEEDS_HUMAN_REVIEW]),
        "paid_total": total(by[Outcome.PAID]),
        "rejected_total": total(by[Outcome.REJECTED]),
        "review_total": total(by[Outcome.NEEDS_HUMAN_REVIEW]),
        # Resolved with no human: paid, or rejected on hard evidence.
        "straight_through_rate": round((len(by[Outcome.PAID]) + len(by[Outcome.REJECTED])) / n, 3)
        if n else 0.0,
        "escalation_rate": round(len(by[Outcome.NEEDS_HUMAN_REVIEW]) / n, 3) if n else 0.0,
        "llm_calls": len(usage),
        "llm_calls_per_invoice": round(len(usage) / n, 2) if n else 0.0,
        "input_tokens": sum(u.input_tokens for u in usage),
        "output_tokens": sum(u.output_tokens for u in usage),
        "estimated_llm_cost": round(cost, 4) if cost is not None else None,
        "cost_per_invoice": round(cost / n, 4) if cost is not None and n else None,
        "avg_seconds_per_invoice": round(sum(r.duration_ms for r in results) / n / 1000, 3) if n else 0.0,
    }


def print_result(console: Console, r: Result, detail: bool = True) -> None:
    color, label = STYLE[r.outcome]
    inv = r.invoice
    warn = " [yellow]⚠[/]" if r.warnings else ""
    head = (f"[bold {color}]{label:<12}[/]{warn} {escape(r.file)}  "
            f"{escape(str(inv.invoice_number or '?'))} · {escape(str(inv.vendor or 'unknown vendor'))} "
            f"· {money(inv.total)}")
    if not detail:
        console.print(head)
        return
    lines = [f"[dim]path:[/] {escape(' → '.join(r.path))}"]
    if r.rationale:
        lines.append(f"[dim]why:[/]  {escape(r.rationale)}")
    for reason in r.reasons:
        style = SEVERITY_STYLE.get(reason["severity"], "")
        tag = " [dim](vendor can correct)[/]" if reason.get("disputable") else ""
        lines.append(f"  [{style}]{reason['severity']:<8}[/] {escape(reason['code'])}: "
                     f"{escape(reason['message'])}{tag}")
    for w in r.warnings:
        lines.append(f"[yellow]⚠ {escape(w)}[/]")
    if r.payment and r.payment.status == "success":
        lines.append(f"[dim]payment:[/] {escape(str(r.payment.reference))}")
    if r.approval:
        for step in r.approval.transcript:
            who = step["role"]
            text = step.get("rationale") or step.get("reasoning", "")
            lines.append(f"[dim]{who} (round {step['round']}):[/] {escape(text)}")
        for call in r.approval.tool_calls:
            lines.append(f"[dim]tool:[/] {call['tool']}({escape(str(call['args']))})")
    console.print(Panel("\n".join(lines), title=head, title_align="left", border_style=color))


def print_summary(console: Console, s: dict) -> None:
    table = Table(title="Run summary", show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Invoices", str(s["invoices"]))
    table.add_row("Paid", f"[green]{s['paid']}[/]  ({money(s['paid_total'])})")
    table.add_row("Rejected", f"[red]{s['rejected']}[/]  ({money(s['rejected_total'])} not paid)")
    table.add_row("Needs review", f"[yellow]{s['needs_review']}[/]  ({money(s['review_total'])} held)")
    table.add_row("Straight-through", f"{s['straight_through_rate']:.0%} resolved without a human")
    table.add_row("Escalation rate", f"{s['escalation_rate']:.0%}")
    table.add_row("LLM calls", f"{s['llm_calls']} ({s['llm_calls_per_invoice']}/invoice), "
                               f"{s['input_tokens']:,} in / {s['output_tokens']:,} out tokens")
    if s["estimated_llm_cost"] is not None:
        table.add_row("LLM cost", f"${s['estimated_llm_cost']:.4f} (${s['cost_per_invoice']:.4f}/invoice)")
    table.add_row("Avg time", f"{s['avg_seconds_per_invoice']}s per invoice")
    console.print(table)
