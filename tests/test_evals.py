"""The evaluation harness scores models correctly: it must catch a model that
approves everything, one that holds everything, and one that flip-flops."""

import itertools

import pytest

from invoice_ap.approval import ApproverVerdict, CriticVerdict
from invoice_ap.evals import evaluate, load_cases, render_markdown, summarize
from invoice_ap.mock_llm import MockProvider
from invoice_ap.models import Decision


class FixedModel:
    """Approver always returns `decision`; critic never objects."""

    name, is_mock = "fixed", False

    def __init__(self, decision, cycle=None):
        self.decision, self.cycle, self.usage = decision, cycle, []

    def structured(self, *, role, system, user, schema, tools=(), context=None):
        if role == "critic":
            return CriticVerdict(objection=False, reasoning="fine")
        decision = next(self.cycle) if self.cycle else self.decision
        return ApproverVerdict(decision=decision, rationale="x", cited_findings=[])


def run(factory, runs=2, only=None):
    reports = evaluate(factory, runs=runs, only=only, workers=2)
    return reports, summarize(reports)


def test_cases_are_loaded_from_the_labeled_manifest_plus_the_sample_case():
    files = {c["file"] for c in load_cases()}
    assert "invoice_1010.txt" in files and "tc30_unverifiable_justification.json" in files
    assert all(c["ideal"] for c in load_cases())


def test_every_labeled_case_actually_reaches_the_approver():
    # Raises if a case is stopped earlier by validation or the policy floor.
    run(MockProvider, runs=1)


def test_a_model_that_approves_everything_is_caught_as_unsafe():
    reports, summary = run(lambda: FixedModel(Decision.APPROVE))
    assert summary["unsafe_approvals"] > 0
    by = {r.file: r for r in reports}
    assert by["tc10_price_5pct_over.json"].unsafe == 2          # approved both runs: unsafe
    assert by["tc02_exactly_10k.json"].unsafe == 0              # approving a clean case is fine
    assert by["tc12_total_shortfall.json"].unsafe == 0          # ambiguous case allows approve


def test_a_model_that_holds_everything_is_over_cautious_but_safe():
    _, summary = run(lambda: FixedModel(Decision.ESCALATE))
    assert summary["unsafe_approvals"] == 0 and summary["over_cautious"] > 0
    assert summary["ideal_rate"] < 1


def test_flip_flopping_across_runs_is_reported_as_inconsistency():
    cycle = itertools.cycle([Decision.APPROVE, Decision.ESCALATE])
    reports, summary = run(lambda: FixedModel(None, cycle), runs=2, only={"tc10"})
    assert summary["inconsistent_cases"] == 1 and summary["mean_consistency"] == 0.5


def test_critic_activity_is_counted():
    class Objector(FixedModel):
        def structured(self, *, role, **kw):
            if role == "critic":
                return CriticVerdict(objection=True, reasoning="no",
                                     cited_evidence=["NEAR_THRESHOLD", "PAST_DUE", "PRICE_ABOVE_LIST",
                                                     "TOTAL_MISMATCH", "AMBIGUOUS_DATE", "FUTURE_DATED"])
            return super().structured(role=role, **kw)

    _, summary = run(lambda: Objector(Decision.APPROVE), runs=1)
    assert summary["critic_called"] > 0
    assert summary["critic_objected"] > 0 and summary["critic_changed_outcome"] > 0


def test_report_renders_with_the_key_numbers():
    reports, summary = run(MockProvider, runs=1, only={"tc10"})
    text = render_markdown(reports, summary, "mock")
    assert "tc10_price_5pct_over.json" in text and "Unsafe approvals" in text
