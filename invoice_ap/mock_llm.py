"""Offline stand-in for the LLM.

It answers from the structured `context` the agents pass alongside the prompt,
using fixed heuristics. It exists so the whole pipeline runs and is testable
with no API key. It is NOT a measure of how the real model reasons: that has
to be evaluated with a real provider.
"""

import time

from .llm import Usage
from .models import Decision


class MockProvider:
    name = "mock"
    is_mock = True

    def __init__(self):
        self.usage = []

    def structured(self, *, role, system, user, schema, tools=(), context=None):
        handler = getattr(self, f"_{role}", None)
        if handler is None:
            raise NotImplementedError(f"mock has no handler for role '{role}'")
        started = time.perf_counter()
        result = handler(schema, tools, context or {})
        # Count calls like a real provider would (zero tokens) so reports are comparable.
        self.usage.append(Usage(role=role, model="mock",
                                latency_ms=(time.perf_counter() - started) * 1000,
                                tool_calls=0))
        return result

    # -- approver ---------------------------------------------------------------

    def _approver(self, schema, tools, ctx):
        by_name = {t.name: t for t in tools}
        packet = ctx["packet"]
        inv, findings = packet["invoice"], packet["findings"]
        warnings = [f for f in findings if f["severity"] == "WARNING"]
        codes = [f["code"] for f in warnings]
        total = inv["total"] or 0

        def verdict(decision, rationale, confidence=0.7):
            return schema(decision=decision, rationale=rationale,
                          cited_findings=codes, confidence=confidence)

        if not warnings:
            return verdict(Decision.APPROVE,
                           f"No findings; total ${total:,.2f} passes every check.", 0.9)

        for f in warnings:
            if f["code"] == "TOTAL_MISMATCH" and "overcharge" in f["message"]:
                return verdict(Decision.ESCALATE, "Total exceeds the sum of lines, tax and "
                               "shipping: an overcharge a human should dispute.")
            if f["code"] == "PRICE_BELOW_LIST":
                return verdict(Decision.ESCALATE, "Unexplained price well below catalog; "
                               "could indicate a wrong item.")
            if f["code"] == "NEAR_THRESHOLD":
                history = by_name["get_vendor_history"].fn(vendor=inv["vendor"])
                others = [h for h in history["invoices"]
                          if h["invoice_number"] != inv["invoice_number"]]
                combined = sum(h["total"] or 0 for h in others) + total
                if others and combined >= 10_000:
                    return verdict(Decision.ESCALATE,
                                   f"{len(others)} other recent invoice(s) from this vendor bring "
                                   f"the combined total to ${combined:,.2f}: possible split billing.")
            if f["code"] == "FUTURE_DATED":
                return verdict(Decision.ESCALATE, "The invoice is dated in the future.")
            if f["code"] == "AMBIGUOUS_DATE":
                return verdict(Decision.ESCALATE, "A date can be read two ways.")

        for f in warnings:
            if f["code"] != "PRICE_ABOVE_LIST":
                continue
            # Check the catalog, but a note on the line is the sender's own claim, not
            # verification (there is no PO to check it against), so never accept it.
            list_price = by_name["get_catalog_price"].fn(item=f.get("item")).get("unit_price")
            return verdict(Decision.ESCALATE,
                           f"Price above the ${list_price:,.2f} catalog price; any explanation on "
                           "the line is an unverified claim from the vendor.")

        return verdict(Decision.APPROVE,
                       "Remaining flags are explained or immaterial; verified catalog pricing "
                       f"with the lookup tool. Total ${total:,.2f}.", 0.7)

    # -- critic -----------------------------------------------------------------

    def _critic(self, schema, tools, ctx):
        draft = ctx["draft"]
        findings = ctx["packet"]["findings"]
        unaddressed = [f["code"] for f in findings
                       if f["severity"] == "WARNING" and f["code"] not in draft.cited_findings]
        if unaddressed:
            return schema(objection=True, cited_evidence=unaddressed,
                          reasoning="Findings were not addressed in the rationale.")
        return schema(objection=False, reasoning="The approval addresses every flag.")

    # -- notifier ---------------------------------------------------------------

    def _notifier(self, schema, tools, ctx):
        from .notify import render_template

        subject, body = render_template(ctx["facts"])
        return schema(subject=subject, body=body)
