# Process Notes: Invoice Handling in Manufacturing AP

Domain research that drives the design of the validation and approval rules. Where a number is an assumption rather than an industry standard, it says so.

## 1. Where invoicing sits in procure-to-pay

1. Requisition: someone requests materials.
2. Purchase order (PO): Acme commits to specific items, quantities and prices.
3. Goods receipt (GRN): the warehouse confirms what physically arrived.
4. Vendor invoice: the vendor bills for what they shipped.
5. Match, approve, pay: AP verifies, an approver signs off, treasury pays (typically on Net-30 terms).

**Simplification in this prototype:** there are no PO or goods-receipt tables, so the inventory database stands in for them. We treat it as "goods that have been ordered, delivered and are on hand". "Quantity exceeds stock" is then a proxy for "billed more than was received". This is our interpretation; the case README only says to flag quantity exceeding available stock. Production extension: add PO and receipt tables and move to a true 3-way match.

**Known limitation:** paying an invoice does not decrement stock, so two invoices can each pass against the same received goods (INV-1001, INV-1006 and INV-1010 bill 10 + 5 + 12 WidgetA against 15 in stock). A production system would keep a ledger of matched quantities. We do not decrement because results would then depend on processing order.

## 2. What inventory validation looks like (3-way match)

Checks AP performs: item exists, quantity ≤ ordered/received, unit price within tolerance, line math and totals reconcile, vendor is approved, invoice is not a duplicate, no invalid data (negative or missing values).

### Hard-fail vs. flag

Hard fails reject immediately with an explanation. Flags pass forward as evidence for the approver.

| Check | Result | Rationale |
|---|---|---|
| Unknown item | Hard fail | Nothing to match against; cannot be a legitimate shipment of our goods |
| Zero stock / out of stock (FakeItem) | Hard fail | Cannot have shipped what we do not hold |
| Quantity > stock, **summed per item across all lines** | Hard fail | INV-1013 bills WidgetA on three lines (15 + 5 + 2 = 22) against stock of 15; per-line checks would miss this |
| Negative quantity or negative total | Hard fail | Data integrity; credit memos are a different document type |
| Missing vendor or invoice number | Hard fail | Cannot be paid or audited without them |
| Exact duplicate of an already-processed invoice | Hard fail | Duplicate payment is a classic AP loss |
| Revised invoice (same number and vendor, different content) | Human review | Revisions are rare and risky, so automating them saves little and risks a lot. Show the delta and the original's status (INV-1004 / revised) |
| Vendor matches only a former name or alias (INV-1012: "formerly FastShip Ltd.") | Flag | Needs confirmation and a vendor-master update; also an impersonation vector |
| Same invoice number, different vendor | Flag | ID collision or fraud |
| Same vendor, items and total as an earlier invoice under a different number | Flag, always escalated | Duplicate billing under a new number is a classic AP loss; the duplicate check keys on the number, so this needs its own check |
| Invoice dated after the as-of date | Flag | Work cannot be billed before it exists; left to the approver because a one-day lead can be a timezone artifact |
| Unit price deviates from catalog beyond tolerance | Flag | Price drift is often legitimate (discounts, e.g. INV-1013 volume discount) |
| Unknown or unapproved vendor | Flag | New vendors are legitimate sometimes, but are the entry point for vendor-impersonation fraud |
| Line math or total does not reconcile | Flag | Could be tax, freight or an extraction error |
| Due date missing, before the invoice date, or in the past | Flag | Fraud/pressure signal or data error (INV-1002, INV-1003, INV-1009) |
| Urgency language ("URGENT", "Immediate", wire-only) | Flag | Pressure to bypass controls is a hallmark of payment fraud |
| Non-USD currency | Flag | Needs an FX decision; out of scope for the prototype |
| Total just under an approval threshold (within 5% below, e.g. $9,900 / $9,975 against $10K) | Flag | Invoices sitting just under a limit can indicate split or structured billing to dodge scrutiny. INV-1008 ($9,900) and INV-1012 ($9,975) fit this pattern |

### Principle for hard fails, and false-reject guards

Hard-fail only when the invoice cannot be legitimate under any interpretation (negative values, missing vendor, zero-stock item, exact duplicate, billing beyond goods received). Anything caused by our own uncertainty (parsing, name matching, units, currency) becomes a flag or goes to a human. A false approve loses money; a false reject is recoverable but still costs delay and a vendor dispute.

Each reject reason carries a `disputable` marker. Stock shortfalls and unknown items are disputable (the vendor can correct the bill or the shipment may be pending); negative totals and duplicates are not. The notification explains how to fix it.

Guards found in the sample data:

- **Item names:** normalize case and whitespace only ("Widget A" in INV-1012 = WidgetA). No fuzzy matching, because WidgetC is one edit from WidgetA.
- **OCR-style typos:** `26-Jan-2O26` and `$3,500.O0` (letter O) in INV-1012 must not fail grounding.
- **Past-due checks:** use a configurable as-of date. Every sample is dated January 2026, so the wall clock would flag nearly all of them. Resolve "yesterday" (INV-1003) against the invoice date.
- **Absent fields:** a field missing from the source (INV-1009 vendor) is never "re-extracted"; the LLM would invent it.
- **Other traps:** INV-1007's total is wrong (14,750 + 885 tax = 15,635, stated 15,525); INV-1013's total is also wrong (21,040 + 1,472.80 tax = 22,512.80, stated 22,562.80, a $50 overcharge; found by the validator, not by hand); INV-1010 bills a rush WidgetA at $300 vs. $250 catalog (+20%); INV-1014 is in EUR with prices 5–10% under catalog.

### Price tolerance

Industry practice is a percentage tolerance, often combined with a dollar cap where the tighter limit governs. A percentage alone is risky on high-value lines. Examples in the sources use about 2%. **Our setting is asymmetric, because overpaying is the loss.** Above catalog price: flag over 2%, with a dollar cap per line (configurable). Below catalog price: flag only over 10%; smaller discounts are INFO (INV-1013's 4% volume discounts should not flag). The 2% is an illustrative convention from vendor documentation, not a standard.

Invoices over $50K escalate to a CFO tier rather than being rejected: rejecting a legitimate large invoice only delays it.

## 3. What the VP approver is actually checking

The VP is not re-checking arithmetic. They are answering:

1. Is it legitimate? (real vendor, no fraud signals)
2. Is it authorized and within budget for this amount?
3. Are the flagged price and quantity exceptions acceptable?
4. What is the cost of being wrong? Paying is hard to reverse; holding an invoice is cheap. Uncertainty resolves to escalate, never to pay.

### Draft approval rules

| Condition | Decision |
|---|---|
| Validation hard-failed | Reject (never reaches the VP) |
| Clean (no flags) and ≤ $10,000 | Approved by code with a templated rationale; no LLM |
| Any flag, or total > $10,000 | Approver and critic loop |
| Fraud-type flag (urgency language, bad due date, unknown vendor, price outliers) | Policy gate forces escalation to a human whatever the LLM said |

Rationale for skipping the LLM on clean invoices: with no flags and a moderate amount there is nothing to weigh, so an LLM adds cost, latency and non-determinism without adding information. Reasoning is reserved for ambiguous evidence. The earlier $1K auto-approve tier is dropped: with no flags it no longer changes the path.

The critique loop: the approver drafts a decision and rationale, the critic objects only by citing a specific flag or fact from the evidence, and the approver revises or stands firm. Capped at 2 rounds in code. If the approver reverses a prior decision, or the two still disagree, escalate to a human. We report the escalation rate so a critic that objects to everything shows up as a number.

## 4. Evidence and honest limits on the thresholds

**There is no authoritative industry benchmark for approval thresholds.** Searching turned up only vendor guidance and illustrative examples. The $10K extra-scrutiny limit (the case README's own example) is therefore a **policy parameter, not a fact**: they live in config, are stated as assumptions in the README, and should be set from the company's own invoice volumes, risk tolerance and audit requirements.

What does support the design:

- **Published examples span this range.** A guide for 100–250 employee companies suggests starting with auto-approval under $500; an Epicor-related glossary gives a $5,000 manager limit with finance-director approval above that. The $10K limit sits within that spread.
- **Our own data.** The 20 sample invoices run from about $1.9K to $100K, with most between $2K and $16K. **None of the invoices that pass validation is over $10K**, so the extra-scrutiny path is not exercised by the samples. We add test cases (with new SKUs, since the seed stock is small) so it is.
- **Cost economics.** Industry-average processing cost is roughly $9–10 per invoice (best-in-class about $2.65–$2.82; manual or bottom performers $10–$20+). A human touch is cheap relative to a large invoice but expensive relative to a small one, which is the logic for auto-approving the low end and spending attention at the high end.
- **Baseline context.** The 30% error rate in the case is far above the figures in these sources (for example about 2% manual error rate; 18.4% average exception rate, about 9% for top performers), which supports the claim that the process is badly broken.
- **Fraud context.** AFP's 2025 survey (data for 2024, 521 practitioners): 79% of organizations experienced attempted or actual payments fraud; business email compromise was the top avenue (63%); vendor impersonation 45%, up 11 points; wires were the payment method most targeted by BEC (63%). ACFE reports billing schemes among the most common forms of asset misappropriation with a median loss around $100K (2020 edition). This supports treating unknown vendors, urgency language and wire-only requests as escalation triggers.

Caveat: most cost and exception figures come from vendor blogs with commercial interests and differing definitions. Treat them as directional, and cite the original Ardent Partners / APQC / AFP / ACFE reports if used in the presentation.

## Sources

- [AI Invoice Processing Benchmarks 2026 (Parseur)](https://parseur.com/blog/ai-invoice-processing-benchmarks)
- [AP Benchmarks Every Modern Team Should Know in 2025 (Ascend)](https://www.ascendsoftware.com/blog/what-good-looks-like-ap-benchmarks-every-modern-team-should-know-in-2025)
- [Accounts Payable KPIs 2026 (Kognitos)](https://www.kognitos.com/blog/accounts-payable-kpis-metrics-that-matter-2026/)
- [Three-Way Matching in Accounts Payable (Paystream Advisors)](https://paystreamadvisors.com/blog/three-way-matching/)
- [Three-way matching policies (Microsoft Dynamics 365)](https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/three-way-matching-policies)
- [Three Way Matching Process in AP (Softco)](https://softco.com/blog/three-way-matching-process-in-ap-common-problems-and-solutions/)
- [Invoice approval threshold and escalation rules (Peakflo)](https://peakflo.co/blog/invoice-approval-threshold-escalation-rules)
- [What are Epicor AP Approval Limits? (Hyperbots)](https://www.hyperbots.com/glossary/epicor-ap-approval-limits)
- [2025 AFP Payments Fraud and Control Survey highlights (Truist)](https://truist.com/content/dam/truist-bank/us/en/documents/info/cci/2025-afp-payments-fraud-control-survey-report-key-highlights.pdf)
- [AFP press release: 79% of organizations were victims of payments fraud in 2024](https://www.financialprofessionals.org/about/learn-more/press-releases/Details/survey-79-percent-of-organizations-were-victims-of-attempted-or-actual-payments-fraud-activity-in-2024)
- [ACFE 2020 Report to the Nations summary (Withum)](https://www.withum.com/resources/2020-report-to-the-nations-insights-and-lessons-on-occupational-fraud/)

## 5. Evidence from running the system

See [eval-results.md](eval-results.md) for the model evaluation: the labeled judgment cases rerun five times each on a live model. Two design gaps (the approver having no way to check a vendor's other invoices, and the critic not seeing the approver's lookups) were found by reading the critic's objections in an earlier run and fixed before the final numbers.
