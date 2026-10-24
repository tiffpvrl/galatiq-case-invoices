"""Model evaluation harness for the approval stage.

Reruns the cases that reach the approver several times and scores the model
against the case author's labeled ideal decisions. It isolates approval (the
approver and critic) from extraction and notification, so a run is cheap and
the numbers say something about judgment, not parsing.

Metrics, per case and in total:
  ideal rate       share of runs whose decision is in the labeled ideal set
  unsafe approvals the model approved where approving is not an ideal answer
                   (the costly error: money paid that should have been held)
  over-caution     the model held a case whose only ideal answer is approve
  consistency      how often runs agree with their own most common decision
  critic           how often it was invoked, objected, and changed the outcome
"""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .approval import run_approval
from .config import DATA_DIR, INVOICE_DIR, SETTINGS, Settings
from .db import init_db
from .llm import Provider, estimated_cost
from .ledger import record
from .models import Decision, Outcome
from .policy import needs_reasoning, rule_floor
from .readers import read_invoice
from .validate import hard_fails, validate

MANIFEST = DATA_DIR / "test_cases" / "expected.json"

# A sample-set judgment case: the "rush order" surcharge is only the sender's claim.
SAMPLE_CASES = [{"file": "invoice_1010.txt", "dir": INVOICE_DIR,
                 "ideal": ["ESCALATE", "REJECT"],
                 "why": "20% rush surcharge justified only by the sender's own note"}]


@dataclass
class CaseRun:
    decision: str
    rounds: int = 0
    critic_called: bool = False
    critic_objected: bool = False
    overturned: bool = False
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    latency_ms: float = 0.0
    llm_error: str | None = None
    # What the critic argued, and how the approver answered, when it objected.
    critic_reasoning: str | None = None
    final_rationale: str | None = None


@dataclass
class CaseReport:
    file: str
    why: str
    ideal: list[str]
    runs: list[CaseRun] = field(default_factory=list)

    @property
    def decisions(self) -> Counter:
        return Counter(r.decision for r in self.runs)

    @property
    def ideal_rate(self) -> float:
        return sum(r.decision in self.ideal for r in self.runs) / len(self.runs)

    @property
    def unsafe(self) -> int:
        return sum(r.decision == "APPROVE" and "APPROVE" not in self.ideal for r in self.runs)

    @property
    def over_cautious(self) -> int:
        return sum(r.decision != "APPROVE" for r in self.runs) if self.ideal == ["APPROVE"] else 0

    @property
    def consistency(self) -> float:
        return max(self.decisions.values()) / len(self.runs)


def load_cases(only: set[str] | None = None) -> list[dict]:
    manifest = json.loads(MANIFEST.read_text())["cases"]
    cases = [{"file": c["file"], "dir": MANIFEST.parent, "ideal": c["ideal"], "why": c["why"],
              "prior": c.get("prior", [])} for c in manifest if c.get("ideal")] + SAMPLE_CASES
    if only:
        cases = [c for c in cases if any(o in c["file"] for o in only)]
    return cases


def _one_run(case: dict, provider: Provider, settings: Settings) -> CaseRun:
    conn = init_db(":memory:")
    try:
        for prior in case.get("prior", []):  # history the case depends on, already paid
            record(conn, "history", read_invoice(Path(case["dir"]) / prior), Outcome.PAID, [])
        inv = read_invoice(Path(case["dir"]) / case["file"])
        findings = validate(inv, conn, settings)
        floor = rule_floor(inv, findings, settings)
        if hard_fails(findings) or floor.decision != Decision.APPROVE \
                or not needs_reasoning(inv, findings + floor.findings, settings):
            raise ValueError(f"{case['file']} does not reach the approver "
                             f"(floor: {floor.decision.value} {floor.triggers})")
        result = run_approval(inv, findings, conn, provider, settings)
    finally:
        conn.close()
    draft_approved = any(s["role"] == "approver" and s["round"] == 0
                         and s["decision"] == "APPROVE" for s in result.transcript)
    critic_steps = [s for s in result.transcript if s["role"] == "critic"]
    return CaseRun(
        decision=result.decision.value,
        rounds=result.rounds,
        critic_called=draft_approved,
        critic_objected=any(s["objection"] for s in critic_steps),
        overturned=result.overturned,
        tool_calls=len(result.tool_calls),
        input_tokens=sum(u.input_tokens for u in provider.usage),
        output_tokens=sum(u.output_tokens for u in provider.usage),
        llm_calls=len(provider.usage),
        latency_ms=round(sum(u.latency_ms for u in provider.usage), 1),
        llm_error=result.llm_error,
        critic_reasoning=next((s["reasoning"] for s in critic_steps if s["objection"]), None),
        final_rationale=result.rationale or None,
    )


def evaluate(
    provider_factory: Callable[[], Provider],
    runs: int = 3,
    only: set[str] | None = None,
    workers: int = 4,
    settings: Settings = SETTINGS,
) -> list[CaseReport]:
    """Each run gets a fresh provider so token and call counts attribute cleanly."""
    cases = load_cases(only)
    reports = [CaseReport(c["file"], c["why"], c["ideal"]) for c in cases]
    jobs = [(i, c) for i, c in enumerate(cases) for _ in range(runs)]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [(i, pool.submit(_one_run, c, provider_factory(), settings)) for i, c in jobs]
        for i, future in futures:
            reports[i].runs.append(future.result())
    return reports


def summarize(reports: list[CaseReport]) -> dict:
    runs = [r for rep in reports for r in rep.runs]
    n = len(runs)
    critic_called = sum(r.critic_called for r in runs)
    total_cost = None
    if runs and runs[0].llm_calls:
        from .llm import Usage

        total_cost = estimated_cost([Usage("x", "x", sum(r.input_tokens for r in runs),
                                           sum(r.output_tokens for r in runs))])
    return {
        "cases": len(reports),
        "runs": n,
        "ideal_rate": round(sum(r.decision in rep.ideal for rep in reports for r in rep.runs) / n, 3),
        "unsafe_approvals": sum(rep.unsafe for rep in reports),
        "over_cautious": sum(rep.over_cautious for rep in reports),
        "inconsistent_cases": sum(len(rep.decisions) > 1 for rep in reports),
        "mean_consistency": round(sum(rep.consistency for rep in reports) / len(reports), 3),
        "critic_called": critic_called,
        "critic_objected": sum(r.critic_objected for r in runs),
        "critic_changed_outcome": sum(r.critic_called and r.overturned for r in runs),
        "llm_error_runs": sum(bool(r.llm_error) for r in runs),
        "avg_llm_calls": round(sum(r.llm_calls for r in runs) / n, 2),
        "avg_tool_calls": round(sum(r.tool_calls for r in runs) / n, 2),
        "avg_latency_s": round(sum(r.latency_ms for r in runs) / n / 1000, 2),
        "input_tokens": sum(r.input_tokens for r in runs),
        "output_tokens": sum(r.output_tokens for r in runs),
        "estimated_cost": round(total_cost, 4) if total_cost is not None else None,
    }


def render_markdown(reports: list[CaseReport], summary: dict, provider: str) -> str:
    runs = summary["runs"] // summary["cases"]
    lines = [
        f"# Approval evaluation: {provider}",
        "",
        f"{summary['cases']} judgment cases x {runs} runs each. Labels are the case author's "
        "judgment, not ground truth; ambiguous cases accept more than one decision.",
        "",
        "| Case | Ideal | Decisions (runs) | Ideal rate | Unsafe | Critic obj. |",
        "|---|---|---|---|---|---|",
    ]
    for rep in reports:
        dist = ", ".join(f"{d} x{n}" for d, n in rep.decisions.most_common())
        objections = sum(r.critic_objected for r in rep.runs)
        lines.append(f"| {rep.file} | {'/'.join(rep.ideal)} | {dist} | "
                     f"{rep.ideal_rate:.0%} | {rep.unsafe} | {objections} |")
    s = summary
    lines += [
        "",
        f"- **Ideal rate:** {s['ideal_rate']:.0%} of runs",
        f"- **Unsafe approvals:** {s['unsafe_approvals']} (approved where holding was ideal)",
        f"- **Over-caution:** {s['over_cautious']} runs held a case whose only ideal answer is approve",
        f"- **Consistency:** {s['inconsistent_cases']} of {s['cases']} cases produced different "
        f"decisions across runs (mean self-agreement {s['mean_consistency']:.0%})",
        f"- **Critic:** invoked on {s['critic_called']} drafted approvals, objected "
        f"{s['critic_objected']} time(s), changed the outcome {s['critic_changed_outcome']} time(s)",
        f"- **Cost/latency per case-run:** {s['avg_llm_calls']} LLM calls, "
        f"{s['avg_tool_calls']} tool calls, {s['avg_latency_s']}s; "
        f"{s['input_tokens']:,} input / {s['output_tokens']:,} output tokens in total"
        + (f"; about ${s['estimated_cost']:.4f}" if s["estimated_cost"] is not None else ""),
    ]
    objections = [(rep.file, r.critic_reasoning) for rep in reports for r in rep.runs
                  if r.critic_reasoning]
    if objections:
        lines += ["", "## Critic objections", ""]
        for file, reasoning in objections:
            lines.append(f"- **{file}:** {reasoning.strip()}")
    if s["llm_error_runs"]:
        lines.append(f"- **Runs with model errors (escalated by the safety net):** {s['llm_error_runs']}")
    return "\n".join(lines) + "\n"


def to_json(reports: list[CaseReport], summary: dict, provider: str) -> str:
    return json.dumps({"provider": provider, "summary": summary,
                       "cases": [{"file": r.file, "why": r.why, "ideal": r.ideal,
                                  "runs": [asdict(x) for x in r.runs]} for r in reports]},
                      indent=2)
