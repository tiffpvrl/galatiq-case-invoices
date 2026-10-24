"""A self-contained HTML report for one run: the AP queues, built for the people who work them.

The design (layout, type scale, colors, interaction model) comes from a design
pass; this module generates the markup from real run data. One file, no network,
no dependencies. Everything that came from an invoice is HTML-escaped (invoice
text is untrusted), and the per-invoice detail panels live in inert <template>
elements that the small script in report_assets/report.js clones on selection.
"""

import datetime as dt
import json
from collections import Counter
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from .models import Outcome
from .pipeline import Result
from .policy import FRAUD_SIGNALS
from .routing import ROLES

ASSETS = Path(__file__).parent / "report_assets"

TONE = {Outcome.NEEDS_HUMAN_REVIEW: "review", Outcome.PAID: "paid", Outcome.REJECTED: "rejected"}
GLYPH = {"review": "ring", "paid": "check", "rejected": "x"}

# Plain-language one-liners for the queue rows; the full message is in the detail panel.
PHRASES = {
    "PRICE_ABOVE_LIST": "Priced above catalog",
    "PRICE_BELOW_LIST": "Priced well below catalog",
    "NEAR_THRESHOLD": "Just under an approval limit",
    "VENDOR_RENAMED": "Vendor name differs from the master",
    "UNKNOWN_VENDOR": "Vendor not in the vendor master",
    "NON_SETTLEABLE_CURRENCY": "Billed in a currency we cannot settle",
    "REVISION_OF_PRIOR": "Revision of an earlier invoice",
    "POSSIBLE_REBILL": "Same bill as an earlier invoice",
    "INVOICE_NUMBER_COLLISION": "Invoice number used by another vendor",
    "PRESSURE_LANGUAGE": "Urgency or authority language",
    "IMMEDIATE_TERMS": "Demands immediate payment",
    "RELATIVE_DUE_DATE": "Due date given as a relative word",
    "DUE_BEFORE_ISSUE": "Due date is before the invoice date",
    "TOTAL_MISMATCH": "Total does not match the lines",
    "SUBTOTAL_MISMATCH": "Subtotal does not match the lines",
    "LINE_AMOUNT_MISMATCH": "A line amount does not match",
    "INSUFFICIENT_STOCK": "Billed more than was received",
    "OUT_OF_STOCK": "Item has no stock on hand",
    "STOCK_CONTENTION": "Stock already claimed by other invoices",
    "UNKNOWN_ITEM": "Item not in the catalog",
    "DUPLICATE_INVOICE": "Identical to an invoice already processed",
    "MISSING_VENDOR": "No vendor on the invoice",
    "MISSING_INVOICE_NUMBER": "No invoice number",
    "MISSING_TOTAL": "No total on the invoice",
    "NON_POSITIVE_TOTAL": "Total is zero or negative",
    "NEGATIVE_QUANTITY": "Negative quantity",
    "NO_LINE_ITEMS": "No line items found",
    "EXTRACTION_FAILED": "The file could not be read",
    "OVER_CFO_THRESHOLD": "Over the CFO approval limit",
    "FUTURE_DATED": "Dated in the future",
    "AMBIGUOUS_DATE": "A date can be read two ways",
    "MISSING_DUE_DATE": "No due date",
    "PAST_DUE": "Already past due",
    "PAYMENT_FAILED": "The payment call failed",
    "INTERNAL_ERROR": "Unexpected error while processing",
}

GROUPS = {
    # key: (label, note, glyph)
    "fraud": ("Blocked: possible fraud", "Do not contact these senders. No correction request was drafted.", "tri"),
    "fix": ("Fixable by vendor", "Correction requests are drafted for review.", "x"),
    "dup": ("Duplicates", "Copies of invoices already processed. No action needed.", "x"),
    "other": ("Invalid, not correctable", "Nothing the vendor can fix as billed.", "x"),
}
GROUP_ORDER = ["fraud", "fix", "dup", "other"]

METHOD_TEXT = {
    "parser": "Structured file parser",
    "rules": "Rule-based text extractor",
    "llm": "LLM extraction",
    "rules-fallback": "Rule-based extractor (LLM unavailable)",
    "llm-fallback": "LLM extraction (parser failed)",
}

SEVERITY = {
    "CRITICAL": ("Critical", "rejected", "tri"),
    "WARNING": ("Warning", "review", "diamond"),
    "INFO": ("Info", "muted", "dot"),
}


def e(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def money(value, cents: bool = False) -> str:
    if value is None:
        return "n/a"
    text = f"{abs(value):,.2f}" if cents else f"{abs(value):,.0f}"
    return f"-${text}" if value < 0 else f"${text}"


def day(value) -> str:
    if not value:
        return "—"
    return f"{value:%b} {value.day}, {value.year}" if isinstance(value, dt.date) else str(value)


def _seconds(value: float) -> str:
    return f"{value * 1000:.0f} ms" if value < 0.1 else f"{value:.1f} s"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def glyph(name: str, tone: str) -> str:
    return (f'<span class="ap-tone-{tone}" style="display: inline-flex"><span class="ap-glyph '
            f'ap-glyph--{name}" aria-hidden="true"></span></span>')


# ---- per-invoice view -----------------------------------------------------------------


@dataclass
class View:
    key: str
    r: Result
    tone: str
    group: str | None = None
    owner: str | None = None
    decider: str | None = None
    codes: list = field(default_factory=list)
    phrase: str = ""
    meta: str = ""
    status: str = ""

    @property
    def number(self) -> str:
        return self.r.invoice.invoice_number or "No number"

    @property
    def vendor(self) -> str:
        return self.r.invoice.vendor or "Unknown vendor"

    @property
    def total(self):
        return self.r.invoice.total


def _group(r: Result, codes: list) -> str:
    if "DUPLICATE_INVOICE" in codes:
        return "dup"
    if FRAUD_SIGNALS & set(codes):
        return "fraud"
    if r.vendor_draft is not None:
        return "fix"
    return "other"


def _view(i: int, r: Result) -> View:
    tone = TONE[r.outcome]
    reasons = r.reasons
    codes = [x["code"] for x in reasons]
    v = View(key=f"i{i}", r=r, tone=tone, codes=codes)
    if r.outcome == Outcome.NEEDS_HUMAN_REVIEW:
        v.owner = r.owner or "AP clerk"
        v.decider = next((c for c in r.owner_why if c.isupper()), None) or (codes[0] if codes else None)
        v.meta, v.status = v.owner, "Needs review"
    elif r.outcome == Outcome.REJECTED:
        v.group = _group(r, codes)
        critical = [x["code"] for x in reasons if x["severity"] == "CRITICAL"]
        v.decider = (critical or codes or [None])[0]
        v.meta = {"fraud": "Do not contact", "fix": "Draft ready", "dup": "No action",
                  "other": "Not correctable"}[v.group]
        v.status = f"Rejected: {GROUPS[v.group][0].replace('Blocked: ', '').lower()}"
    else:
        v.meta, v.status = "Paid", "Ready to pay"
    if v.decider:
        message = next((x["message"] for x in reasons if x["code"] == v.decider), "")
        v.phrase = PHRASES.get(v.decider) or (message[:70] + ("…" if len(message) > 70 else ""))
    elif r.outcome == Outcome.PAID:
        v.phrase = "All checks passed"
    return v


# ---- queue rows -----------------------------------------------------------------------


def _row(v: View) -> str:
    search = " ".join(str(x or "") for x in (v.number, v.vendor, v.owner, v.phrase, v.status,
                                              v.r.file, *v.codes)).lower()
    prefix = "Suggested owner: " if v.owner else ""
    return (
        f'<li class="ap-row-item" data-key="{v.key}" data-owner="{e(v.owner or "")}" '
        f'data-search="{e(search)}"><button type="button" class="ap-row" aria-current="false" '
        f'data-key="{v.key}"><span class="ap-row-id">{e(v.number)}</span>'
        f'<span class="ap-row-vendor" title="{e(v.vendor)}">{e(v.vendor)}</span>'
        f'<span class="ap-row-amt">{e(money(v.total, True))}</span>'
        f'<span class="ap-row-reason">{e(v.phrase)}</span>'
        f'<span class="ap-row-meta"><span class="ap-sr">{prefix}</span>{e(v.meta)}</span>'
        f'<span class="ap-row-st ap-tone-{v.tone}"><span class="ap-glyph ap-glyph--{GLYPH[v.tone]}" '
        f'aria-hidden="true"></span>{e(v.status)}</span></button></li>')


def _review_section(held: list) -> str:
    total = sum(max(v.total or 0, 0) for v in held)
    head = (f'<div class="ap-section-h"><h2 id="ap-rev-h" class="ap-h2">Needs review</h2>'
            f'<span class="ap-section-n">{len(held)} · {money(total)}</span></div>')
    if not held:
        return ('<section class="ap-section" data-queue aria-labelledby="ap-rev-h">' + head +
                '<p class="ap-empty">Nothing is waiting for a person. Every invoice in this run was '
                'paid or rejected automatically.</p></section>')
    owners = [o for o in reversed(ROLES) if any(v.owner == o for v in held)]
    chips = ['<button type="button" class="ap-chip" aria-pressed="true" data-owner="all">'
             f'<span>All</span><span class="ap-chip-n">{len(held)}</span>'
             f'<span class="ap-chip-amt">{money(total)}</span></button>']
    for o in owners:
        mine = [v for v in held if v.owner == o]
        chips.append(
            f'<button type="button" class="ap-chip" aria-pressed="false" data-owner="{e(o)}">'
            f'<span>{e(o)}</span><span class="ap-chip-n">{len(mine)}</span>'
            f'<span class="ap-chip-amt">{money(sum(max(v.total or 0, 0) for v in mine))}</span></button>')
    return ('<section class="ap-section" data-queue aria-labelledby="ap-rev-h">' + head +
            '<div class="ap-chips" role="group" aria-label="Filter by suggested owner">'
            + "".join(chips) + '</div>'
            '<ul class="ap-rows" id="ap-review-rows" data-ctx="Needs review">'
            + "".join(_row(v) for v in held) + '</ul>'
            '<div class="ap-more" data-more hidden><button type="button" class="ap-btn">Show more'
            '</button></div></section>')


def _group_block(dom: str, label: str, note: str, glyph_name: str, tone: str, count: int,
                 amount: float, rows: list, top: bool = False) -> str:
    tag = "h2" if top else "h3"
    cls = "ap-group-btn ap-group-btn--top" if top else "ap-group-btn"
    heading = (f'<{tag} class="ap-h3" style="color: inherit; font-size: inherit; font-weight: inherit">'
               if not top else f'<{tag} class="ap-h2">')
    return (f'<div class="ap-group">{heading}<button type="button" class="{cls}" aria-expanded="false" '
            f'aria-controls="{dom}"><span class="ap-caret" aria-hidden="true"></span>'
            f'<span class="ap-group-glyph ap-tone-{tone}"><span class="ap-glyph ap-glyph--{glyph_name}" '
            f'aria-hidden="true"></span></span><span class="ap-group-label">{e(label)}</span>'
            f'<span class="ap-group-meta">{count} · {money(amount)}</span>'
            f'<span class="ap-group-note">{e(note)}</span></button></{tag}>'
            f'<ul id="{dom}" class="ap-rows is-collapsed" data-ctx="{e(label)}">'
            + "".join(_row(v) for v in rows) + '</ul></div>')


def _rejected_section(rejected: list) -> str:
    if not rejected:
        return ""
    amount = sum(max(v.total or 0, 0) for v in rejected)
    blocks = []
    for key in GROUP_ORDER:
        rows = [v for v in rejected if v.group == key]
        if rows:
            label, note, g = GROUPS[key]
            blocks.append(_group_block(f"ap-grp-{key}", label, note, g, "rejected", len(rows),
                                       sum(max(v.total or 0, 0) for v in rows), rows))
    return ('<section class="ap-section" data-queue aria-labelledby="ap-rej-h"><div class="ap-section-h">'
            f'<h2 id="ap-rej-h" class="ap-h2">Rejected</h2><span class="ap-section-n">{len(rejected)} '
            f'· {money(amount)} not paid</span></div>' + "".join(blocks) + '</section>')


def _paid_section(paid: list) -> str:
    if not paid:
        return ""
    return ('<section class="ap-section" data-queue aria-label="Ready to pay">'
            + _group_block("ap-paid-rows", "Ready to pay",
                           "Approved automatically. No action needed.", "check", "paid", len(paid),
                           sum(max(v.total or 0, 0) for v in paid), paid, top=True)
            + '</section>')


# ---- detail panel ---------------------------------------------------------------------


def _steps(r: Result) -> list:
    """(label, state, text) for the five stages. state: ok | held | block | skip."""
    path = r.path
    failed = "ingest:failed" in path
    steps = [("Ingest", "block" if failed else "ok",
              "Could not read" if failed else METHOD_TEXT.get(r.method, r.method).lower())]
    validate = next((p for p in path if p.startswith("validate:")), None)
    if failed or validate is None:
        steps.append(("Validate", "skip", ""))
    else:
        hard = int(validate.split(":")[1].split(" ")[0])
        steps.append(("Validate", "block" if hard else "ok", plural(hard, "hard fail") if hard else "passed"))
    floor = next((p for p in path if p.startswith("floor:")), None)
    if floor is None:
        steps.append(("Policy floor", "skip", ""))
    elif floor.endswith("ESCALATE"):
        steps.append(("Policy floor", "held", "Escalated"))
    elif floor.endswith("REJECT"):
        steps.append(("Policy floor", "block", "Rejected"))
    else:
        steps.append(("Policy floor", "ok", "passed"))
    approver = next((p for p in path if p.startswith("approver:")), None)
    if approver:
        decision = approver.split(":")[1].split(" ")[0]
        state, text = {"APPROVE": ("ok", "Approved"), "ESCALATE": ("held", "Held"),
                       "REJECT": ("block", "Rejected")}.get(decision, ("ok", decision))
        steps.append(("Approver", state, text))
    elif "auto-approve" in path:
        steps.append(("Auto-approve", "ok", "no flags"))
    else:
        steps.append(("Approver", "skip", ""))
    if "pay" in path:
        steps.append(("Pay", "ok", "Paid"))
    elif "pay:failed" in path:
        steps.append(("Pay", "block", "Failed"))
    else:
        steps.append(("Pay", "skip", ""))
    return steps


STEP_GLYPH = {"ok": ("check", "paid"), "held": ("ring", "review"), "block": ("x", "rejected"),
              "skip": ("dash", "faint")}


def _path_html(r: Result) -> str:
    items = []
    for label, state, text in _steps(r):
        g, tone = STEP_GLYPH[state]
        text_html = f'<span class="ap-step-state">{e(text)}</span>' if text else ""
        items.append(f'<li class="ap-step ap-step--{state}">{glyph(g, tone)}'
                     f'<span class="ap-step-label">{e(label)}</span>{text_html}</li>')
    return f'<ol class="ap-path">{"".join(items)}</ol>'


def _kv(v: View) -> list:
    inv = v.r.invoice
    due = day(inv.due_date) if inv.due_date else (inv.due_date_text or "—")
    if v.r.outcome == Outcome.NEEDS_HUMAN_REVIEW:
        kv = [("Suggested owner", v.owner, ""), ("Decided by", v.decider or "—", "ap-mono")]
        others = [c for c in v.codes if c != v.decider]
        if others:
            kv.append(("Also flagged", ", ".join(others), "ap-mono"))
        return kv + [("Due", due, "")]
    if v.r.outcome == Outcome.PAID:
        ref = v.r.payment.reference if v.r.payment else None
        return [("Payment", ref or "—", ""), ("Due", due, "")]
    can = {"fix": "Yes, request drafted", "fraud": "No. Do not contact",
           "dup": "No action: already processed", "other": "No: not correctable as billed"}[v.group]
    kv = [("Vendor can correct", can, ""), ("Decided by", v.decider or "—", "ap-mono")]
    others = [c for c in v.codes if c != v.decider]
    if others:
        signals = [c for c in v.codes if c in FRAUD_SIGNALS]
        if v.group == "fraud" and signals:
            kv.append(("Fraud flags", str(len(signals)), ""))
        else:
            kv.append(("Also flagged", ", ".join(others), "ap-mono"))
    return kv


def _reasons_html(reasons: list) -> str:
    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    items = []
    for x in sorted(reasons, key=lambda x: order.get(x["severity"], 3)):
        label, tone, g = SEVERITY.get(x["severity"], SEVERITY["INFO"])
        fix = " (the vendor can correct this)" if x.get("disputable") else ""
        items.append(f'<li><span class="ap-sev ap-tone-{tone}"><span class="ap-glyph ap-glyph--{g}" '
                     f'aria-hidden="true"></span>{label}</span><code class="ap-code">{e(x["code"])}</code>'
                     f'<span class="ap-reason-msg">{e(x["message"])}{e(fix)}</span></li>')
    return f'<ul class="ap-reasons">{"".join(items)}</ul>' if items else \
        '<p class="ap-muted">No flags.</p>'


def _lines_html(v: View) -> str:
    inv = v.r.invoice
    rows = "".join(
        f'<div role="row"><span role="cell">{e(li.item)}</span><span role="cell" class="num">{li.quantity:g}'
        f'</span><span role="cell" class="num">{e(money(li.unit_price, True))}</span>'
        f'<span role="cell" class="num">{e(money(li.amount, True))}</span>'
        f'<span role="cell" class="ap-note">{e(li.note)}</span></div>' for li in inv.line_items)
    if not rows:
        return '<p class="ap-muted">No line items were found.</p>'
    return (f'<div class="ap-tablebox"><div class="ap-gt ap-gt--lines" role="table" '
            f'aria-label="Line items for {e(v.number)}"><div role="row"><span role="columnheader">Item</span>'
            '<span role="columnheader" class="num">Qty</span><span role="columnheader" class="num">Unit price'
            '</span><span role="columnheader" class="num">Amount</span><span role="columnheader">Note</span>'
            f'</div>{rows}<div role="row"><span role="rowheader" class="is-total">Invoice total</span>'
            '<span role="cell" class="is-total"></span><span role="cell" class="is-total"></span>'
            f'<span role="cell" class="num is-total">{e(money(inv.total, True))}</span>'
            '<span role="cell" class="is-total"></span></div></div></div>')


def _facts(v: View) -> list:
    r, inv = v.r, v.r.invoice
    ms = r.duration_ms
    if r.payment and r.payment.status == "success":
        payment = r.payment.reference
    else:
        payment = "None (not paid)"
    return [("Invoice date", day(inv.date)), ("Due date", day(inv.due_date) if inv.due_date
                                               else (inv.due_date_text or "—")),
            ("Terms", inv.payment_terms or "—"), ("Currency", inv.currency),
            ("Read by", METHOD_TEXT.get(r.method, r.method or "—")),
            ("Processing time", f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"),
            ("Model calls", str(r.llm_calls)), ("Payment reference", payment)]


def _draft(note) -> str:
    return f"To: {note.to}\nSubject: {note.subject}\n\n{note.body}"


def _disclosures(v: View) -> str:
    r = v.r
    turns = r.approval.transcript if r.approval else []
    tools = r.approval.tool_calls if r.approval else []
    if turns:
        items = []
        for t in turns:
            extra = ""
            if t["role"] == "critic":
                extra = " · objected" if t.get("objection") else " · no objection"
            elif t.get("decision"):
                extra = f" · {t['decision']}"
            text = t.get("rationale") or t.get("reasoning") or ""
            items.append(f'<li><p class="ap-turn-role">{e(t["role"].title())} · round {t["round"]}'
                         f'{e(extra)}</p><p class="ap-turn-text">{e(text)}</p></li>')
        turns_html = f'<ol class="ap-turns">{"".join(items)}</ol>'
    else:
        turns_html = ('<p class="ap-muted">No model reasoning: deterministic checks decided this '
                      'invoice.</p>')
    if tools:
        rows = "".join(
            f'<div><dt>{e(c["tool"])}({e(json.dumps(c["args"], default=str))})</dt>'
            f'<dd>{e(_short(json.dumps(c["result"], default=str)))}</dd></div>' for c in tools)
        lookups_html = f'<dl class="ap-lookups">{rows}</dl>'
    else:
        lookups_html = '<p class="ap-muted">The approver was not asked, so no lookups ran.</p>'
    if r.warnings:
        warns = "".join(f'<li>{glyph("dot", "muted")}<span>{e(w)}</span></li>' for w in r.warnings)
        warn_html = f'<ul class="ap-warns">{warns}</ul>'
    else:
        warn_html = '<p class="ap-muted">None. The run handled this invoice without operational issues.</p>'
    parts = [
        f'<details class="ap-disc"><summary>Approver and critic reasoning '
        f'<span class="ap-disc-n">{len(turns)}</span></summary><div class="ap-disc-body">{turns_html}</div></details>',
        f'<details class="ap-disc"><summary>Lookups the approver ran <span class="ap-disc-n">{len(tools)}'
        f'</span></summary><div class="ap-disc-body">{lookups_html}</div></details>',
        f'<details class="ap-disc"><summary>Operational warnings <span class="ap-disc-n">{len(r.warnings)}'
        f'</span></summary><div class="ap-disc-body">{warn_html}</div></details>',
    ]
    if r.notification:
        parts.append(f'<details class="ap-disc"><summary>Draft: notify {e(r.notification.to)}</summary>'
                     f'<div class="ap-disc-body"><pre class="ap-draft">{e(_draft(r.notification))}</pre></div></details>')
    if r.vendor_draft:
        parts.append('<details class="ap-disc"><summary>Draft: correction request to vendor</summary>'
                     f'<div class="ap-disc-body"><pre class="ap-draft">{e(_draft(r.vendor_draft))}</pre></div></details>')
    else:
        note = {"fraud": "No vendor draft: this sender should not be contacted until a person decides.",
                "dup": "No vendor draft: the original invoice was already processed.",
                "other": "No vendor draft: nothing the vendor can correct as billed."}.get(
            v.group or "", "No vendor draft: this invoice was not rejected.")
        parts.append(f'<p class="ap-disc-note">{e(note)}</p>')
    return f'<div class="ap-discs">{"".join(parts)}</div>'


def _short(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _detail(v: View) -> str:
    r, inv = v.r, v.r.invoice
    status_label = v.status
    g = GLYPH[v.tone]
    kv = "".join(f'<div><dt>{e(k)}</dt><dd class="{c}">{e(val)}</dd></div>' for k, val, c in _kv(v))
    facts = "".join(f"<div><dt>{e(k)}</dt><dd>{e(val)}</dd></div>" for k, val in _facts(v))
    currency = f'<span class="ap-dcur">{e(inv.currency)}, not converted</span>' if inv.currency != "USD" else ""
    return (
        '<div class="ap-d"><div class="ap-dbar">'
        '<button type="button" class="ap-btn ap-back" data-close><svg width="14" height="14" viewBox="0 0 16 16" '
        'fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M10 3L5 8l5 5"></path>'
        '</svg><span>Back to list</span></button>'
        f'<span class="ap-dstatus ap-tone-{v.tone}"><span class="ap-glyph ap-glyph--{g}" aria-hidden="true">'
        f'</span>{e(status_label)}</span><div class="ap-dnav"><span class="ap-dpos" data-pos></span>'
        '<button type="button" class="ap-btn ap-iconbtn" aria-label="Previous invoice" data-prev>'
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" '
        'aria-hidden="true"><path d="M3 10l5-5 5 5"></path></svg></button>'
        '<button type="button" class="ap-btn ap-iconbtn" aria-label="Next invoice" data-next>'
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" '
        'aria-hidden="true"><path d="M3 6l5 5 5-5"></path></svg></button>'
        '<button type="button" class="ap-btn ap-iconbtn ap-close" aria-label="Close detail" data-close>'
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" '
        'aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"></path></svg></button></div></div>'
        f'<div class="ap-dhead"><p class="ap-did"><span class="ap-mono">{e(v.number)}</span>'
        f'<span aria-hidden="true">·</span><span class="ap-dfile">{e(r.file)}</span></p>'
        f'<h2 class="ap-dvendor" tabindex="-1">{e(v.vendor)}</h2>'
        f'<p class="ap-damt">{e(money(inv.total, True))} {currency}</p><dl class="ap-dkv">{kv}</dl></div>'
        f'<div class="ap-dsec"><h3 class="ap-h3">Why</h3><p class="ap-why">{e(r.rationale) or "—"}</p></div>'
        f'<div class="ap-dsec"><h3 class="ap-h3">Decision path</h3>{_path_html(r)}</div>'
        f'<div class="ap-dsec"><h3 class="ap-h3">Reasons <span class="ap-h3-n">{len(r.reasons)}</span></h3>'
        f'{_reasons_html(r.reasons)}</div>'
        f'<div class="ap-dsec"><h3 class="ap-h3">Line items</h3>{_lines_html(v)}</div>'
        f'<div class="ap-dsec"><h3 class="ap-h3">Facts</h3><dl class="ap-facts">{facts}</dl></div>'
        f'{_disclosures(v)}</div>')


# ---- summary --------------------------------------------------------------------------


def _mix(segments: list, mode: str, n: int, total: float) -> str:
    """One bar plus legend for 'dollars' or 'count'."""
    def pct(value, whole):
        if not whole:
            return "0%"
        p = value / whole * 100
        return "<1%" if 0 < p < 1 else f"{round(p)}%"

    bars, legend, aria = [], [], []
    for label, tone, g, items in segments:
        amount = sum(max(v.total or 0, 0) for v in items)
        count = len(items)
        count_text = plural(count, "invoice")
        grow = max(amount, 0.0001) if mode == "dollars" else count
        primary = money(amount) if mode == "dollars" else count_text
        secondary = (f"{count_text} · {pct(amount, total)}" if mode == "dollars"
                     else f"{money(amount)} · {pct(count, n)}")
        bars.append(f'<span class="ap-mix-seg ap-tone-{tone}" style="flex-grow: {grow}"></span>')
        legend.append(f'<li><span class="ap-legend-label">{glyph(g, tone)}{e(label)}</span>'
                      f'<span class="ap-legend-val">{e(primary)}</span>'
                      f'<span class="ap-legend-sub">{e(secondary)}</span></li>')
        aria.append(f"{label} {primary}")
    hidden = "" if mode == "dollars" else " hidden"
    return (f'<div data-mode="{mode}"{hidden}><div class="ap-mix" role="img" aria-label="Outcome by {mode}: '
            f'{e("; ".join(aria))}">{"".join(bars)}</div><ul class="ap-legend">{"".join(legend)}</ul></div>')


def _summary(views: list, results: list, held: list, paid: list, rejected: list) -> str:
    n = len(views)
    total = sum(max(v.total or 0, 0) for v in views)
    segments = [s for s in (("Needs review", "review", "ring", held), ("Ready to pay", "paid", "check", paid),
                            ("Rejected, not paid", "rejected", "x", rejected)) if s[3]]
    auto = n - len(held)
    if n == 1:
        headline = ("The one invoice in this run was resolved without a person." if auto
                    else "The one invoice in this run needs a person.")
    else:
        headline = f"{round(auto / n * 100) if n else 0}% of {n} invoices were resolved without a person."
    fraud = [v for v in rejected if v.group == "fraud"]
    dups = [v for v in rejected if v.group == "dup"]
    if held:
        unit = "invoice to review" if len(held) == 1 else "invoices to review"
        focus = (f'<p class="ap-eyebrow">{glyph("ring", "review")}Needs review</p>'
                 f'<p class="ap-display">{len(held)} <span class="ap-display-unit">{unit}</span></p>'
                 f'<p class="ap-held">{money(sum(max(v.total or 0, 0) for v in held))} held for a person</p>')
    else:
        focus = (f'<p class="ap-eyebrow">{glyph("check", "paid")}Needs review</p>'
                 '<p class="ap-display">Nothing to review</p>')
    if fraud:
        who = plural(len(fraud), "invoice")
        alert = ('<div class="ap-alert" role="note">' + glyph("tri", "rejected") +
                 f'<p><strong>Possible fraud.</strong> {who} blocked, '
                 f'{money(sum(max(v.total or 0, 0) for v in fraud))}. Do not contact these senders.</p>'
                 '<button type="button" class="ap-btn" data-fraud-btn>Show</button></div>')
    else:
        alert = f'<p class="ap-calm">{glyph("check", "muted")}No fraud flags in this run.</p>'
    twin = "".join(
        f'<div role="row"><span role="rowheader">{e(label)}</span><span role="cell" class="num">{len(items)}'
        f'</span><span role="cell" class="num">{e(money(sum(max(v.total or 0, 0) for v in items)))}</span>'
        f'<span role="cell" class="num">'
        f'{round(sum(max(v.total or 0, 0) for v in items) / total * 100) if total else 0}%</span></div>'
        for label, _, _, items in segments)
    calls = sum(r.llm_calls for r in results)
    per = calls / n if n else 0
    avg = sum(r.duration_ms for r in results) / n / 1000 if n else 0
    dup_text = f"{len(dups)} · {money(sum(max(v.total or 0, 0) for v in dups))}" if dups else "None"
    return (
        '<section class="ap-summary" aria-labelledby="ap-sum-h"><h2 id="ap-sum-h" class="ap-sr">Run summary</h2>'
        f'<div class="ap-focus">{focus}<p class="ap-lede">{e(headline)}</p>{alert}</div>'
        '<div class="ap-mixwrap"><div class="ap-mixhead"><h3 class="ap-h3">Outcome by '
        '<span data-mode-word>dollars</span></h3><div class="ap-seg" role="group" aria-label="Measure outcomes by">'
        '<button type="button" aria-pressed="true" data-mode-btn="dollars">Dollars</button>'
        '<button type="button" aria-pressed="false" data-mode-btn="count">Count</button></div></div>'
        f'{_mix(segments, "dollars", n, total)}{_mix(segments, "count", n, total)}'
        '<details class="ap-twin"><summary>View as table</summary><div class="ap-gt ap-gt--mix" role="table" '
        'aria-label="Outcomes by dollars and count"><div role="row"><span role="columnheader">Outcome</span>'
        '<span role="columnheader" class="num">Invoices</span><span role="columnheader" class="num">Dollars</span>'
        f'<span role="columnheader" class="num">Share of $</span></div>{twin}<div role="row">'
        f'<span role="rowheader" class="is-total">Total</span><span role="cell" class="num is-total">{n}</span>'
        f'<span role="cell" class="num is-total">{e(money(total))}</span><span role="cell" class="num is-total">'
        '100%</span></div></div></details></div>'
        '<dl class="ap-stats">'
        f'<div><dt>Duplicate copies caught</dt><dd>{e(dup_text)}</dd></div>'
        f'<div><dt>Model calls</dt><dd>{calls} · {per:.1f} per invoice</dd></div>'
        f'<div><dt>Average time per invoice</dt><dd>{_seconds(avg)}</dd></div></dl></section>')


def _about(results: list, provider_name: str) -> str:
    n = len(results)
    usage = [u for r in results for u in r.usage]
    roles = Counter(u.role for u in usage)
    breakdown = ", ".join(f"{role} {count}" for role, count in sorted(roles.items()))
    tokens_in = sum(u.input_tokens for u in usage)
    tokens_out = sum(u.output_tokens for u in usage)
    text = f"{len(usage)} calls · {len(usage) / n:.1f} per invoice" if n else "0 calls"
    if breakdown:
        text += f" · {breakdown}"
    if tokens_in or tokens_out:
        text += f" · {tokens_in:,} tokens in, {tokens_out:,} out"
    from .llm import estimated_cost

    cost = estimated_cost(usage)
    if cost is not None and n:
        text += f" · about ${cost:.4f} (${cost / n:.4f} per invoice)"
    return (
        '<footer class="ap-about" aria-labelledby="ap-about-h"><h2 id="ap-about-h" class="ap-h2">About this run</h2>'
        f'<dl class="ap-about-dl"><div><dt>Model usage</dt><dd>{e(text)}</dd></div>'
        f'<div><dt>Provider</dt><dd>{e(provider_name)}</dd></div>'
        f'<div><dt>Invoices processed</dt><dd>{n}</dd></div></dl>'
        '<ul class="ap-caveats"><li>If this is the provided sample set, about half the invoices are deliberately '
        'bad, so these figures show the controls working.</li><li>They are not a savings estimate.</li></ul>'
        '</footer>')


# ---- the page -------------------------------------------------------------------------


def build_report(results: list, run_id: str, provider_name: str, is_mock: bool, as_of: dt.date,
                 generated: dt.datetime | None = None) -> str:
    generated = generated or dt.datetime.now()
    views = [_view(i, r) for i, r in enumerate(results)]
    views.sort(key=lambda v: (v.r.invoice.invoice_number or "￿", v.r.file))
    held = [v for v in views if v.r.outcome == Outcome.NEEDS_HUMAN_REVIEW]
    held.sort(key=lambda v: (-ROLES.index(v.owner or "AP clerk"), v.number))  # most senior first
    rejected = [v for v in views if v.r.outcome == Outcome.REJECTED]
    paid = [v for v in views if v.r.outcome == Outcome.PAID]

    mock = ('<p class="ap-notice"><span class="ap-tone-review" style="display: inline-flex"><span class="ap-glyph '
            'ap-glyph--diamond" aria-hidden="true"></span></span>Offline mock run: model reasoning is canned</p>'
            if is_mock else "")
    templates = "".join(f'<template id="d-{v.key}">{_detail(v)}</template>' for v in views)
    templates += ('<template id="d-none"><div class="ap-dempty"><p>Select an invoice to see why it was held, '
                  'the evidence, and the drafts.</p></div></template>')
    search_results = (
        '<section class="ap-section" data-results hidden aria-labelledby="ap-res-h"><div class="ap-section-h">'
        '<h2 id="ap-res-h" class="ap-h2">Search results</h2><span class="ap-section-n" data-count></span></div>'
        '<ul class="ap-rows is-results" data-ctx="results"></ul><p class="ap-empty" data-none hidden>No invoices '
        'match “<span data-q></span>”. Try an invoice number, vendor, owner or flag code.</p></section>')

    body = (
        f'<div class="ap" data-theme="system"><div class="ap-wrap no-sel">'
        '<header class="ap-header"><div class="ap-heading"><h1 class="ap-h1">Invoice run</h1><p class="ap-meta">'
        f'<span>Run <span class="ap-mono">{e(run_id)}</span></span><span class="ap-sep" aria-hidden="true">·</span>'
        f'<span>{e(day(generated.date()))}</span><span class="ap-sep" aria-hidden="true">·</span>'
        f'<span>Provider {e(provider_name)}</span><span class="ap-sep" aria-hidden="true">·</span>'
        f'<span>Past due as of {e(day(as_of))}</span></p>{mock}</div><div class="ap-tools"><div class="ap-search">'
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" '
        'aria-hidden="true"><circle cx="7" cy="7" r="4.75"></circle><path d="M10.5 10.5L14 14"></path></svg>'
        '<label class="ap-sr" for="ap-q">Search invoices</label><input id="ap-q" type="search" '
        'placeholder="Search invoice, vendor, owner, flag" autocomplete="off"></div>'
        '<button type="button" class="ap-btn" data-theme-btn aria-label="Theme: System. Switch theme">'
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
        'aria-hidden="true"><circle cx="8" cy="8" r="6"></circle><path d="M8 2a6 6 0 0 0 0 12z" fill="currentColor">'
        '</path></svg><span>System</span></button></div></header>'
        '<p class="ap-sr" aria-live="polite" data-live></p>'
        '<noscript><p class="ap-notice">This report needs JavaScript to open invoice details.</p></noscript>'
        f'{_summary(views, results, held, paid, rejected)}'
        '<div class="ap-main"><div class="ap-list">'
        f'{search_results}{_review_section(held)}{_rejected_section(rejected)}{_paid_section(paid)}</div>'
        '<aside class="ap-detail" aria-label="Invoice detail"></aside></div>'
        f'{_about(results, provider_name)}{templates}</div></div>')
    css = (ASSETS / "report.css").read_text(encoding="utf-8")
    js = (ASSETS / "report.js").read_text(encoding="utf-8")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Invoice run {e(run_id)}</title><style>body{{margin:0}}\n{css}</style></head>'
            f'<body>{body}<script>{js}</script></body></html>')


def write_report(path: Path, results: list, run_id: str, provider_name: str, is_mock: bool,
                 as_of: dt.date) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(results, run_id, provider_name, is_mock, as_of), encoding="utf-8")
    return path
