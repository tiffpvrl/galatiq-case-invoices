# Acme Corp invoice processing: a multi-agent AP system

Acme Corp is a PE-backed manufacturer losing about $2M a year to manual invoice processing: a 30% error rate, five-day delays, and approval by email chain. This repo is a working prototype that automates the whole path. It reads an invoice in any of five formats, checks it against inventory and vendor records, decides whether to pay, and either pays it, rejects it with reasons, or hands it to a person with the evidence.

The original case brief is in [docs/CASE.md](docs/CASE.md).

**The short version**

- **Most of the system is plain code, on purpose.** Exact checks (arithmetic, stock, duplicates) run as code. An LLM is used only where there is ambiguity: reading messy text, weighing flagged invoices, and a second-opinion review. A clean invoice costs zero model calls.
- **The model can only make a decision more cautious.** A deterministic policy floor runs first and the model can never override it.
- **Evaluated, not just demoed.** 70 live runs of the judgment cases produced 0 unsafe approvals, and 247 tests pass.

## Business impact

**What the design choices mean for the business**

| Design choice | Business effect |
|---|---|
| Plain code for every exact check; AI only where there is ambiguity | Predictable cost and auditable decisions: the AI is used only where it adds something. |
| A policy floor the AI cannot override | The AI can never approve past a fraud flag or an approval limit, which is what auditors need to see. |
| Reject only what cannot be legitimate; everything else is a flag | Fewer wrongful rejections (each one is a vendor dispute) without letting bad invoices through. |
| The AI's reading is checked against the source, with retries | Attacks the 30% error rate directly: values that are not in the document are caught before validation. |
| Duplicate, revision and re-bill checks; idempotent payment | Prevents duplicate payments, a classic AP loss. A revised invoice pays only the difference. |
| Held invoices routed to the right owner, with the evidence attached | Attacks the five-day delay: exceptions reach Treasury, the buyer or the controller directly instead of waiting in a shared inbox. |
| Every decision and every human override recorded | An audit trail, and data for tuning the rules from real reviewer behavior. |

**What we measured** on the provided sample. It is deliberately about half bad, so these figures show the controls working, not what Acme would save:

- 80% of invoices resolved without a person; the rest went to review with the reasons and evidence attached.
- About 4 seconds per invoice end to end, against a baseline of days.
- About $181K of bad invoices stopped, and 0 unsafe approvals in 70 evaluation runs.
- 1.3 model calls per invoice, about 36K input and 18K output tokens for all 20. Set `AP_PRICE_IN_PER_MTOK` and `AP_PRICE_OUT_PER_MTOK` in `.env` and each run reports dollars per invoice.

**Sizing the savings for Acme.** Savings per year is approximately

> invoices per year × [ manual cost per invoice − ( touchless share × automated cost + exception share × cost to review ) ] + errors avoided × cost of an error

Every input has to come from Acme: its invoice volume; its team's time per invoice (industry averages are roughly $9 to $10, and about $3 for best-in-class teams); the touchless share measured on its real mix (industry average roughly a third, best-in-class about half); the system's token cost; clerk time per exception; and the cost of an error (rework, overpayment, duplicate payment). The industry figures are vendor-published and directional. Nothing here was run on real volume.

## Quick start

Developed and tested on Python 3.13 (3.10+ should work; only 3.13 was tested).

```bash
git clone https://github.com/tiffpvrl/galatiq-case-invoices.git && cd galatiq-case-invoices
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python main.py --invoice_path=data/invoices/invoice_1001.txt   # one invoice, full detail
python main.py --all --fresh --open                             # all samples, then open the HTML report
python main.py --review                                         # resolve escalated invoices
python main.py --insights                                       # policy suggestions from review decisions
pytest                                                          # 247 tests, about 14 seconds
```

With no API key, everything runs on an **offline mock provider** (rule-based extraction, canned reasoning), so these commands work immediately. For real model reasoning, copy `.env.example` to `.env` and set one of:

| Provider | Setting |
|---|---|
| Grok (the case's specified engine) | `XAI_API_KEY=...` |
| Any OpenAI-compatible endpoint | `LLM_BASE_URL=...`, `LLM_API_KEY=...`, `LLM_MODEL=...` |

I tested the system using Claude, since that was the API available to me. It is built for Grok's API as the case specifies, and runs on any OpenAI-compatible endpoint (see `.env.example`).

Other useful flags: `--json` (machine-readable), `--brief` (one line per invoice), `--offline` (force the mock), `--provider grok|custom|mock`, `--reserve-stock`, `--as-of YYYY-MM-DD`, `-v` (structured trace to stderr), `--open` (open the HTML report), `--no-report`. Each run writes `runs/<run-id>/report.html`, a JSON-lines trace, and the notification drafts (nothing is ever sent).

The SQLite inventory is created and seeded automatically on first run. The four starter rows from the brief are unchanged; three extra SKUs let a clean invoice over $10K exist for testing. As the brief invites, the seed data is extended: `inventory` gains `unit_price` (for price checks), and new tables hold approved vendors and their former names, the processed-invoice ledger, payments and reviewer decisions. Inventory is keyed by item only, so it records how much of an item is on hand, not which vendor delivered it.

## How it works

```mermaid
flowchart TD
    IN([Invoice file]) --> FMT["Format router<br/>(code)"]
    FMT -->|"json, csv, xml"| PARSE["Parser<br/>(code)"]
    FMT -->|"txt, pdf"| EXTRACT["Extractor<br/>(LLM + grounding check)"]
    PARSE -->|"parse error"| EXTRACT
    PARSE --> VALIDATE
    EXTRACT --> VALIDATE
    EXTRACT -->|"cannot read it"| HUMAN

    VALIDATE["Validation (code, SQLite)<br/>duplicates, stock, price, vendor,<br/>dates, fraud signals"]
    VALIDATE -->|"hard fail"| REJECT["Reject, with reasons"]
    VALIDATE -->|"pass"| FLOOR{"Policy floor<br/>(code)"}

    FLOOR -->|"identity or pressure flags,<br/>revisions, FX, over $50K"| HUMAN["Human review queue"]
    FLOOR -->|"clean and under $10K"| AUTO["Auto-approve<br/>(code)"]
    FLOOR -->|"has flags, or $10K+"| APPROVER["Approver<br/>(LLM + tools)"]

    APPROVER -->|"drafts approval"| CRITIC["Critic<br/>(LLM)"]
    CRITIC -->|"cited objection, max 2 rounds"| APPROVER
    APPROVER -->|"escalate"| HUMAN
    APPROVER -->|"reject"| REJECT
    CRITIC -->|"approval holds up"| PAY
    AUTO --> PAY["Payment<br/>(idempotent)"]

    PAY --> NOTIFY
    REJECT --> NOTIFY
    HUMAN --> OWNER["Suggested owner<br/>(routing table)"]
    OWNER --> NOTIFY["Notification drafts, never sent<br/>(LLM for held and rejected,<br/>template for paid)"]
    NOTIFY --> LEDGER[("SQLite ledger<br/>+ JSON trace")]

    LEDGER --> REPORT["AP queues report<br/>(HTML: needs review,<br/>ready to pay, rejected)"]
    LEDGER --> REVIEW["Review queue<br/>(a person approves,<br/>rejects or holds)"]
    REVIEW --> INSIGHTS["Insights<br/>(suggested policy changes)"]
```

Every invoice ends in exactly one of three outcomes: **Paid**, **Rejected** (with reasons), or **Needs human review** (with reasons).

### Agent or plain code?

| Step | Type | Why |
|---|---|---|
| Format routing, json/csv/xml parsing | Code | Structured input: a parser is exact, free and testable |
| Extraction from txt and PDF | **LLM agent** | Typos, free-form layouts and OCR errors defeat regex |
| Grounding check (is every extracted value in the source?) | Code | Normalized string matching beats a model judging a model. Its errors drive the retry |
| Duplicate, revision and rebill checks | Code | Lookups on the ledger |
| Inventory, price, vendor, date and fraud checks | Code | SQL and arithmetic with explicit rules |
| Policy floor | Code | Deterministic. The model cannot override it |
| Approver | **LLM agent** | Weighs mixed evidence, verifies claims with five read-only tools |
| Critic | **LLM agent** | An independent second reviewer who must cite a real finding to object (maker/checker) |
| Payment, rejection | Code | Side effects must be deterministic |
| Notification drafts | **LLM agent** for held and rejected; template for paid | Phrases facts it is given for the person who must act; a paid invoice has nothing to explain, so it costs no model call |
| Suggested owner, vendor correction request, HTML report | Code | A routing table, a template, and a static report: nothing here needs a model |

### Agentic capabilities the brief asks for

- **Function calling.** The approver verifies claims with `get_item_stock`, `get_catalog_price`, `get_vendor`, `get_invoice_history` and `get_vendor_history`.
- **Structured outputs.** Every agent must return an object that validates against a Pydantic schema via a final `submit` call; invalid submissions are sent back for correction.
- **Self-correction loops.** Extraction is checked against the source and re-prompted with the specific errors, up to 3 times. The approver and critic loop is capped at 2 rounds in code.

### Safety properties (each is tested)

- A hard-failed invoice never reaches the approver, and never reaches payment.
- The model can only move a decision toward caution. Final decision = most cautious of the policy floor and the model.
- Any error, whether an LLM outage, a payment failure or an unexpected exception, ends in human review, never in a payment.
- The approver sees structured fields only, never the raw document. Invoice free text is passed as labeled untrusted data, and known injection phrasing is caught in code before any model runs.
- Payment is idempotent. Re-running a file cannot pay twice.
- "Unreadable" is never treated as "invalid". A garbled or truncated file goes to a person, not to rejection.

## How this fits Acme's process

In practice this runs in the background and the AP team works from queues, not from a command line.

| Who | What they use it for |
|---|---|
| **AP clerks** | Work the *needs review* queue. The job shifts from keying data to resolving exceptions |
| **AP manager / controller** | Watch volume, how much escalates, and the money held. Tune the rules using the insights report |
| **VP / CFO** | See only what crosses their approval tier ($10K, and $50K for the CFO) |
| **Treasury** | Handle foreign-currency invoices and release payments |
| **Buyers and vendor management** | Settle price disputes and vendor-master questions routed to them |
| **Auditors** | Read the trace and ledger: who decided what, and why |

**The intended flow.** Invoices arrive in a shared AP inbox, a supplier portal or an EDI feed, and a watcher runs each one through the pipeline as it lands. Every invoice ends in one of three queues:

- **Ready to pay** feeds the next payment run.
- **Needs review** is a work queue; each item names the role best placed to resolve it (Treasury for FX, the buyer for a price dispute, the controller for a suspected re-bill, the CFO above $50K).
- **Rejected** is blocked with its reasons. If every reason is one the vendor can fix, a correction request is drafted for an AP clerk to review; possible fraud is never auto-contacted.

A pop-up per invoice would bury people, so the surface is a queue with counts, an alert only for items needing action now, and a digest for the manager. The system only writes drafts; it never sends anything.

**What exists here and what production adds.**

| This prototype | Production would add |
|---|---|
| A CLI plus a static HTML report of the three queues, with suggested owners | A live workbench with sign-in, roles and an assignment workflow |
| Files read from a folder | Inbox, supplier portal and EDI intake, watching continuously |
| Mock payment, immediate | Scheduled payment runs and a release step by a second person |
| Notification and vendor drafts saved as files | Delivery by email or chat, an SLA clock on each queue, and a daily digest |
| An inventory table standing in for POs and receipts | Real PO and goods-receipt matching, and ERP posting |

## Domain reasoning

The rules come from how accounts payable works, not from guesses. The full write-up, with sources and where the evidence is thin, is in [docs/process-notes.md](docs/process-notes.md). The decisions that matter most:

- **Inventory stands in for the PO and goods receipt.** In a real P2P cycle, AP matches the invoice against the purchase order and the goods receipt (a "3-way match"). The brief provides only an inventory table, so we treat it as "goods ordered and received", and billing more than is on hand is billing beyond what arrived. This is our interpretation.
- **Hard fail vs. flag.** Hard-fail only when an invoice cannot be legitimate under any reading (negative values, missing vendor, an item that doesn't exist, billing beyond received goods, an exact duplicate). Everything caused by *our* uncertainty becomes a flag or goes to a human. A false approve loses money; a false reject costs a vendor dispute.
- **Thresholds are policy parameters, not facts.** There is no authoritative benchmark for approval limits; the sources are vendor guidance with examples between $500 and $5,000. The $10K scrutiny limit is the brief's own example. Everything lives in [invoice_ap/config.py](invoice_ap/config.py).
- **Price tolerance is asymmetric** (flag over 2% above list, but only over 10% below), because overpaying is the loss.
- **Stock is summed across lines.** INV-1013 bills WidgetA on three lines (15 + 5 + 2 = 22) against 15 on hand. Each line fits; the invoice doesn't.

## Results

### The 20 provided invoices

Identical outcomes on a live LLM run and on the offline mock:

| Outcome | Count | Invoices |
|---|---|---|
| **Paid** | 5, $19,140 | 1001, 1004, 1006, 1011 (pdf), 1015 |
| **Needs review** | 4, $27,225 | 1004 revised, 1010, 1012 (pdf), 1014 |
| **Rejected** | 11 | 1002, 1003, 1005, 1007, 1008, 1009, 1013 (json), 1016, plus the repeat copies of 1011, 1012 and 1013 |

The brief's own scenarios all behave as specified (stock mismatch, the zero-stock item, unknown items, the negative quantity). Roughly $181K of billed amounts across the 8 genuinely bad invoices was stopped; the remaining rejections are duplicates of invoices that were paid or reviewed.

Several traps were found in the data that the brief doesn't list:

- **INV-1013's total is wrong by $50** (lines $21,040 + 7% tax = $22,512.80, but it states $22,562.80).
- **INV-1007's total is wrong by $110** ($14,750 + $885 tax = $15,635, but it states $15,525).
- **INV-1008 ($9,900) and INV-1012 ($9,975)** sit just under the $10K scrutiny line.
- **INV-1012** bills under a new company name ("formerly FastShip Ltd.") and has OCR-style typos (`26-Jan-2O26`, `$3,500.O0`).
- **INV-1004** has a revision that, if approved in full, would double-pay: the system pays only the $4,050 difference.

### Model evaluation

A harness ([scripts/eval_judgment.py](scripts/eval_judgment.py)) reruns the 14 cases that reach the approver and scores the model against labeled ideal decisions. Full table: [docs/eval-results.md](docs/eval-results.md).

| Metric (14 cases x 5 live runs) | Result |
|---|---|
| Decisions in the ideal set | 100% of 70 runs |
| Unsafe approvals (approved where holding was ideal) | 0 |
| Over-caution | 0 |
| Run-to-run self-agreement | 99% |
| Paraphrased prompt injection that bypasses the code defense | held in 5 of 5 runs |
| Real split billing across two invoices | escalated in 5 of 5 runs |

The first evaluation scored 95% with an inconsistent case. Reading the critic's objections showed two real design gaps: the approver had no way to look up a vendor's other invoices, yet claimed split billing was unlikely, and the critic couldn't see the approver's lookups. Both were fixed (a `get_vendor_history` tool; the critic now sees the actual lookup results), and the final numbers above are from after those fixes.

## Testing

247 tests, about 14 seconds, no network:

- **Golden outcomes** for all 20 sample files and 31 extra cases ([data/test_cases/](data/test_cases/), generated by [scripts/generate_test_cases.py](scripts/generate_test_cases.py), each with a documented purpose): boundaries at exactly $10,000 and exactly the stock on hand, cross-format duplicates, near-miss item names, malformed files, prompt injection, re-billing under a new number, and more.
- **Agent mechanics** with scripted providers: loop bounds, citation rules, extraction retry, tool-use loops.
- **Wire-level tests** for Grok: a local HTTP server using xAI's documented response shapes, driven by the real `openai` SDK.
- **Safety invariants:** never pay on error, never pay a hard fail, idempotent payment.

## The report and the review queue

Every run writes a self-contained HTML report (`runs/<run-id>/report.html`; no network, works offline), laid out for the person working the queue:

- **A five-second summary:** invoices needing a person and the dollars held, the share resolved without one, a "possible fraud" banner when something was blocked for deception signals, and an outcome bar you can switch between dollars and count.
- **Needs review first,** most senior owner first, with filter chips per suggested owner (CFO through AP clerk) and "Show more" beyond a dozen.
- **Rejections grouped by what can be done about them:** possible fraud (do not contact), fixable by vendor (correction drafted), duplicates, and invalid. **Ready to pay** is collapsed, since clean invoices need no attention.
- **A detail panel** (a full screen on a phone): the plain-language reason, decision path, severity-tagged reasons, line items, facts, and expandable approver and critic reasoning, the lookups it ran, operational warnings, and the drafts.
- Search across every queue, light and dark themes, keyboard operation, a print layout, and "View as table" under the chart.

Invoice text is escaped because it is untrusted, and the interactions are tested in headless Chrome when it is installed. The report is a snapshot; review stays in the terminal on purpose, since a local page that can approve payments is a security surface that does not belong in a prototype.

`python main.py --review` is an interactive queue for escalated invoices: approve, reject or hold, with the reasons, evidence and suggested owner shown. Every decision is stored with a reviewer and note, and approving a revision pays only the difference.

`python main.py --insights` turns those decisions into *suggested* policy changes, such as "reviewers approved every single-flag FX invoice, so consider letting the approver weigh it". It counts only single-flag invoices (otherwise a decision can't be attributed to a flag) and requires a statistical bar, so "5 of 5" doesn't move policy but "12 of 12" does. Suggestions are never applied automatically. There are no real reviewer decisions yet; `scripts/simulate_reviews.py` builds a separate demo database, and the output labels simulated data clearly.

## Limitations and next steps

- **Paying doesn't decrement stock,** so two invoices can each pass against the same received goods. This is deliberate. `--reserve-stock` treats each paid invoice as consuming stock, and on the sample set it cuts paid invoices from 5 to 2: with only 15 WidgetA on hand, 1006, 1011 and 1015 are rejected as stock contention, although the brief treats them as valid. Without PO or receipt numbers the system can't tell a second shipment from a double claim, so the option stays opt-in. A production system would match invoices to receipts and keep a ledger of matched quantities. A softer alternative is to raise contention as a flag for review rather than a rejection.
- **Line notes are read by the model, not by the rules.** Notes such as "Volume discount", "Replacement", "Sample" and "Expedited" (INV-1013) appear in the report and in the approver's evidence, but the deterministic price and stock checks ignore them, so a discounted line can look like a price deviation. It would be interesting to survey the range of notes real invoices carry and how they separate legitimate adjustments from fraud, for example a "sample" line billed at full price.
- **Split billing is partly covered.** The approver can check a vendor's recent invoices, but there is no deterministic cross-invoice rule, and the sample data only exercises it through one constructed case.
- **No FX.** Non-USD invoices are escalated, not converted.
- **The rule-based text extractor was tuned on the ten sample text and PDF files.** On live runs the LLM matched it on every core field, but unseen layouts are the LLM's job and weren't tested on a wider variety.
- **The evaluation is small and the labels are mine.** 14 cases, one provider, and ambiguous cases accept several decisions. It shows the approach works and gives a repeatable method, not a proof.
- **The critic never objected in the final evaluation.** It was valuable for finding the two gaps above; whether it earns its roughly 25% share of approval calls is open, and a config lever can restrict it to high-stakes invoices.
- **A model can reject outright.** The LLM sometimes rejects an overcharged invoice instead of escalating it. Both are safe, but a stricter policy would let only humans reject.

## Repository layout

```
main.py                  CLI: process, --review, --insights
invoice_ap/
  pipeline.py            end-to-end flow for one invoice
  readers.py             json/csv/xml/txt/pdf readers (deterministic)
  text_extractor.py      rule-based extractor (offline mode, fallback)
  extract.py             LLM extraction + grounding retry loop
  grounding.py           is every extracted value in the source?
  validate.py            duplicates, stock, price, vendor, dates, fraud signals
  policy.py              the policy floor the model cannot override
  approval.py            approver + critic reflection loop, tools
  llm.py / mock_llm.py   LLM providers and the offline mock
  payment.py, ledger.py, db.py, tools.py, trace.py
  routing.py, notify.py  suggested owner, notification and vendor drafts
  html_report.py         the AP queues report (markup generated from run data)
  report_assets/         the report's stylesheet and script
  review.py, insights.py human review queue, policy suggestions
  evals.py               model evaluation harness
  config.py              every threshold, with rationale
scripts/                 check_llm, eval_judgment, generate_test_cases, simulate_reviews
data/invoices/           the 20 provided invoices
data/test_cases/         31 extra cases + expected outcomes
tests/                   247 tests
docs/                    CASE.md (the brief), architecture, process-notes, eval-results
```

Design and failure paths: [docs/architecture.md](docs/architecture.md).
