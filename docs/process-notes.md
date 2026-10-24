# Process Notes: Invoice Handling in Manufacturing AP

Domain research that drives the design of the validation and approval rules. Where a number is an assumption rather than an industry standard, it says so.

## 1. Where invoicing sits in procure-to-pay

1. Requisition: someone requests materials.
2. Purchase order (PO): Acme commits to specific items, quantities and prices.
3. Goods receipt (GRN): the warehouse confirms what physically arrived.
4. Vendor invoice: the vendor bills for what they shipped.
5. Match, approve, pay: AP verifies, an approver signs off, treasury pays (typically on Net-30 terms).

**Simplification in this prototype:** there are no PO or goods-receipt tables, so the inventory database stands in for them. "Quantity exceeds stock" is a proxy for "billed more than ordered/received". Production extension: add PO and receipt tables and move to a true 3-way match.

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
| Revised invoice (same number, different content) | Flag | May supersede the earlier one (INV-1004 / revised); needs a human decision |
| Unit price deviates from catalog beyond tolerance | Flag | Price drift is often legitimate (discounts, e.g. INV-1013 volume discount) |
| Unknown or unapproved vendor | Flag | New vendors are legitimate sometimes, but are the entry point for vendor-impersonation fraud |
| Line math or total does not reconcile | Flag | Could be tax, freight or an extraction error |
| Due date missing, before the invoice date, or in the past | Flag | Fraud/pressure signal or data error (INV-1002, INV-1003, INV-1009) |
| Urgency language ("URGENT", "Immediate", wire-only) | Flag | Pressure to bypass controls is a hallmark of payment fraud |
| Non-USD currency | Flag | Needs an FX decision; out of scope for the prototype |
| Total just under an approval threshold (within 5% below, e.g. $9,900 / $9,975 against $10K) | Flag | Invoices sitting just under a limit can indicate split or structured billing to dodge scrutiny. INV-1008 ($9,900) and INV-1012 ($9,975) fit this pattern |

### Price tolerance

Industry practice is a percentage tolerance, often combined with a dollar cap where the tighter limit governs. A percentage alone is risky on high-value lines. Examples in the sources use about 2%. **Our setting: 2% of catalog price, capped at a small absolute amount per line (configurable).** The 2% is an illustrative convention from vendor documentation, not a standard.

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
| Any fraud signal or unresolved flag | Escalate to human |
| Clean and ≤ $1,000 | Auto-approve, pay |
| Clean and $1,000–$10,000 | Approve with a short rationale |
| Clean and > $10,000 | Critic must challenge the draft; approve only if it holds up |

The critique loop: the approver drafts a decision and rationale, the critic argues the opposite, the approver revises or stands firm. Capped at 2 rounds; persistent disagreement escalates to a human.

## 4. Evidence and honest limits on the thresholds

**There is no authoritative industry benchmark for approval thresholds.** Searching turned up only vendor guidance and illustrative examples. The $1K and $10K tiers are therefore **policy parameters, not facts**: they live in config, are stated as assumptions in the README, and should be set from the company's own invoice volumes, risk tolerance and audit requirements.

What does support the design:

- **Published examples span this range.** A guide for 100–250 employee companies suggests starting with auto-approval under $500; an Epicor-related glossary gives a $5,000 manager limit with finance-director approval above that. Our $1K / $10K tiers sit within that spread.
- **Our own data.** The 20 sample invoices run from about $1.9K to $100K, with most between $2K and $16K. **None is under $1K**, so the auto-approve tier will not fire on the samples. We should add a few small invoices to the test set so every tier is exercised.
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
