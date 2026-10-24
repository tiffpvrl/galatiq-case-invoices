"""A self-contained HTML snapshot of the AP queues for one run.

Organized the way an AP team works: a summary for the manager, then the three
queues (needs review, ready to pay, rejected). Each held invoice names the
person who should look at it. One file, no network, no dependencies, so it
opens from a double-click. Everything that came from an invoice is HTML-escaped
(invoice text is untrusted).
"""

import datetime as dt
import json
from collections import defaultdict
from html import escape
from pathlib import Path

from .models import Outcome
from .pipeline import Result
from .report import money, summarize
from .routing import ROLES

OUTCOME_META = {
    # outcome: (css class, icon, label)
    Outcome.PAID: ("paid", "✓", "Paid"),
    Outcome.NEEDS_HUMAN_REVIEW: ("review", "!", "Needs review"),
    Outcome.REJECTED: ("rejected", "✕", "Rejected"),
}
SEVERITY_ICON = {"CRITICAL": "✕", "WARNING": "!", "INFO": "i"}


def e(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def _short_money(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"${value / 1_000:.0f}K"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def _tile_money(value: float) -> str:
    """Whole dollars: cents add nothing to a headline tile and do not fit a phone."""
    return f"-${-value:,.0f}" if value < 0 else f"${value:,.0f}"


def _plural(n: int, word: str = "invoice") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _badge(outcome: Outcome) -> str:
    css, icon, label = OUTCOME_META[outcome]
    return f'<span class="badge {css}"><span class="ico" aria-hidden="true">{icon}</span>{label}</span>'


def _stacked_bar(title: str, parts: list[tuple[str, str, float, str]], total: float, unit: str) -> str:
    """parts: (css class, label, value, formatted value). A thin stacked bar with a 2px gap
    between segments, in-segment labels only when they fit, a legend, and a table twin."""
    segs, legend, rows = [], [], []
    for css, label, value, shown in parts:
        share = value / total if total else 0
        rows.append(f"<tr><th scope='row'>{e(label)}</th><td>{e(shown)}</td><td>{share:.0%}</td></tr>")
        legend.append(f'<li><span class="swatch {css}"></span>{e(label)} '
                      f'<b>{e(shown)}</b> <span class="muted">{share:.0%}</span></li>')
        if value <= 0:
            continue
        inside = f'<span class="seglabel">{e(shown)}</span>' if share >= 0.16 else ""
        segs.append(f'<div class="seg {css}" style="flex:{max(value, total * 0.004)}" tabindex="0" '
                    f'data-tip="{e(label)}: {e(shown)} ({share:.0%} of {e(unit)})">{inside}</div>')
    return (f'<figure class="barchart"><figcaption>{e(title)}</figcaption>'
            f'<div class="bar" role="img" aria-label="{e(title)}">{"".join(segs)}</div>'
            f'<ul class="legend">{"".join(legend)}</ul>'
            f'<details class="tableview"><summary>View as table</summary>'
            f'<table><thead><tr><th>Outcome</th><th>{e(unit.title())}</th><th>Share</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></details></figure>')


def _tile(label: str, value: str, note: str = "", css: str = "") -> str:
    return (f'<div class="tile {css}"><div class="tile-label">{e(label)}</div>'
            f'<div class="tile-value">{e(value)}</div>'
            f'<div class="tile-note">{e(note)}</div></div>')


def _reasons(reasons: list[dict]) -> str:
    if not reasons:
        return '<p class="muted">No flags.</p>'
    items = []
    for r in reasons:
        sev = r.get("severity", "INFO")
        fix = ' <span class="muted">(the vendor can correct this)</span>' if r.get("disputable") else ""
        items.append(f'<li class="reason {e(sev.lower())}"><span class="sev" title="{e(sev)}">'
                     f'{SEVERITY_ICON.get(sev, "i")}</span><div><code>{e(r["code"])}</code> '
                     f'{e(r["message"])}{fix}</div></li>')
    return f'<ul class="reasons">{"".join(items)}</ul>'


def _path(steps: list[str]) -> str:
    return f'<ol class="path" aria-label="Decision path">{"".join(f"<li>{e(s)}</li>" for s in steps)}</ol>'


def _lines(r: Result) -> str:
    if not r.invoice.line_items:
        return ""
    rows = "".join(
        f"<tr><td>{e(li.item)}</td><td class='num'>{li.quantity:g}</td>"
        f"<td class='num'>{money(li.unit_price)}</td><td class='num'>{money(li.amount)}</td>"
        f"<td class='muted'>{e(li.note)}</td></tr>" for li in r.invoice.line_items)
    return ('<h4>Line items</h4><table class="lines"><thead><tr><th>Item</th><th class="num">Qty</th>'
            '<th class="num">Unit price</th><th class="num">Amount</th><th>Note</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def _reasoning(r: Result) -> str:
    if not r.approval:
        return ""
    steps = []
    for s in r.approval.transcript:
        text = s.get("rationale") or s.get("reasoning") or ""
        extra = ""
        if s["role"] == "critic":
            extra = " · objected" if s.get("objection") else " · no objection"
        elif s.get("decision"):
            extra = f" · {s['decision']}"
        steps.append(f'<div class="turn {e(s["role"])}"><div class="who">{e(s["role"].title())}'
                     f' · round {s["round"]}{e(extra)}</div><p>{e(text)}</p></div>')
    tools = ""
    if r.approval.tool_calls:
        rows = "".join(f"<li><code>{e(c['tool'])}({e(json.dumps(c['args'], default=str))})</code></li>"
                       for c in r.approval.tool_calls)
        tools = f'<h4>Lookups the approver ran</h4><ul class="tools">{rows}</ul>'
    return f'<h4>Approver and critic</h4>{"".join(steps)}{tools}'


def _draft(label: str, note) -> str:
    if not note:
        return ""
    return (f'<details class="email"><summary>{e(label)}</summary>'
            f'<pre>To: {e(note.to)}\nSubject: {e(note.subject)}\n\n{e(note.body)}</pre></details>')


def _card(r: Result) -> str:
    css, _, _ = OUTCOME_META[r.outcome]
    inv = r.invoice
    top = next((x for x in r.reasons if x["severity"] != "INFO"), None)
    if r.outcome == Outcome.NEEDS_HUMAN_REVIEW and r.owner:
        last = f'<span class="owner" title="Suggested owner">{e(r.owner)}</span>'
        decider = next((c for c in r.owner_why if c.isupper()), None) or (top["code"] if top else None)
        if decider:
            last += f' <code>{e(decider)}</code>'
    elif r.vendor_draft:
        last = '<span class="muted">vendor can correct</span>'
    else:
        last = f'<code>{e(top["code"])}</code>' if top else '<span class="muted">clean</span>'
    search = " ".join(str(x or "") for x in (r.file, inv.invoice_number, inv.vendor, r.outcome.value,
                                              r.owner, *[x["code"] for x in r.reasons])).lower()
    warn = '<span class="warnmark" title="Operational warning during this run">⚠</span>' if r.warnings else ""
    facts = [("Invoice", inv.invoice_number), ("Vendor", inv.vendor), ("Date", inv.date),
             ("Due", inv.due_date or inv.due_date_text), ("Currency", inv.currency),
             ("Terms", inv.payment_terms), ("Read by", r.method), ("Time", f"{r.duration_ms:.0f} ms"),
             ("Model calls", r.llm_calls)]
    fact_html = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v if v not in (None, '') else '—')}</dd></div>"
                        for k, v in facts)
    owner_line = ""
    if r.outcome == Outcome.NEEDS_HUMAN_REVIEW and r.owner:
        because = f" because of {', '.join(r.owner_why)}" if r.owner_why else ""
        owner_line = f'<p class="why"><b>Suggested owner:</b> {e(r.owner)}{e(because)}</p>'
    payment = (f'<p><b>Payment reference:</b> <code>{e(r.payment.reference)}</code></p>'
               if r.payment and r.payment.status == "success" else "")
    warnings = "".join(f'<p class="warnline">⚠ {e(w)}</p>' for w in r.warnings)
    return (
        f'<details class="inv {css}" data-search="{e(search)}">'
        f'<summary><span class="c-badge">{_badge(r.outcome)}{warn}</span>'
        f'<span class="c-id"><b>{e(inv.invoice_number or "?")}</b><span class="muted">{e(r.file)}</span></span>'
        f'<span class="c-vendor">{e(inv.vendor or "unknown vendor")}</span>'
        f'<span class="c-total num">{money(inv.total)}</span>'
        f'<span class="c-reason">{last}</span></summary>'
        f'<div class="card-body"><p class="why"><b>Why:</b> {e(r.rationale) or "—"}</p>{owner_line}{warnings}'
        f'<h4>Decision path</h4>{_path(r.path)}<h4>Reasons</h4>{_reasons(r.reasons)}'
        f'{_lines(r)}{_reasoning(r)}{payment}<dl class="facts">{fact_html}</dl>'
        f'{_draft("Notification draft (not sent)", r.notification)}'
        f'{_draft("Correction request for the vendor: draft for AP review (not sent)", r.vendor_draft)}'
        f'</div></details>')


def _by_owner(held: list[Result]) -> str:
    totals: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for r in held:
        totals[r.owner or "AP clerk"][0] += 1
        totals[r.owner or "AP clerk"][1] += max(r.invoice.total or 0, 0)
    rows = "".join(
        f"<tr><th scope='row'>{e(role)}</th><td class='num'>{totals[role][0]}</td>"
        f"<td class='num'>{money(totals[role][1])}</td></tr>"
        for role in reversed(ROLES) if role in totals)
    return ('<table class="owners"><thead><tr><th>Suggested owner</th><th class="num">Invoices</th>'
            f'<th class="num">Held</th></tr></thead><tbody>{rows}</tbody></table>')


def _queue(css: str, title: str, blurb: str, results: list[Result], extra: str = "") -> str:
    if not results:
        return (f'<section class="queue {css}"><h2>{e(title)} <span class="count">0</span></h2>'
                f'<p class="muted">Nothing here.</p></section>')
    return (f'<section class="queue {css}"><h2>{e(title)} <span class="count">{len(results)}</span></h2>'
            f'<p class="muted">{e(blurb)}</p>{extra}{"".join(_card(r) for r in results)}</section>')


CSS = """
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--wash:rgba(11,11,11,.04);
--good:#0ca30c;--warn:#fab219;--crit:#d03b3b;--fill-ink:#fff;--warn-ink:#0b0b0b;--accent:#2a78d6;
color-scheme:light}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){--page:#0d0d0d;
--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--ring:rgba(255,255,255,.10);--wash:rgba(255,255,255,.05);--accent:#3987e5;color-scheme:dark}}
:root[data-theme="dark"]{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--wash:rgba(255,255,255,.05);
--accent:#3987e5;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
header.top{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 6px}h4{font-size:12px;margin:18px 0 6px;color:var(--ink2);text-transform:uppercase;letter-spacing:.04em}
.sub,.muted{color:var(--ink2)}.muted{font-size:13px}.num{text-align:right}
button{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--axis);border-radius:8px;padding:6px 12px;cursor:pointer;min-height:36px}
button:hover{background:var(--wash)}
button:focus-visible,summary:focus-visible,input:focus-visible,.seg:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.notice{margin:16px 0 0;padding:10px 14px;border:1px solid var(--axis);border-left:4px solid var(--warn);border-radius:8px;background:var(--surface);font-size:14px}
.hero{display:flex;flex-wrap:wrap;gap:24px;align-items:flex-end;margin:24px 0 8px}
.hero-fig{font-size:56px;line-height:1;font-weight:650;letter-spacing:-.02em}
.hero-cap{max-width:38ch;color:var(--ink2)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px 16px}
.tile{min-width:0}.tile-label{font-size:13px;color:var(--ink2)}.tile-value{font-size:clamp(18px,5.4vw,28px);font-weight:600;margin-top:2px;white-space:nowrap}
.tile-note{font-size:12px;color:var(--muted);min-height:1.2em}
.tile.paid{border-top:3px solid var(--good)}.tile.review{border-top:3px solid var(--warn)}.tile.rejected{border-top:3px solid var(--crit)}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:16px 18px}
.barchart{margin:0 0 20px}.barchart figcaption{font-size:13px;color:var(--ink2);margin-bottom:8px}
.bar{display:flex;gap:2px;height:30px}
.seg{border-radius:4px;display:flex;align-items:center;justify-content:center;min-width:6px;overflow:hidden}
.seglabel{font-size:12px;font-weight:600;padding:0 6px;white-space:nowrap;color:var(--fill-ink)}
.seg.review .seglabel{color:var(--warn-ink)}
.seg.paid,.swatch.paid{background:var(--good)}.seg.review,.swatch.review{background:var(--warn)}.seg.rejected,.swatch.rejected{background:var(--crit)}
.legend{list-style:none;padding:0;margin:8px 0 0;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px}
.swatch{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
.tableview{margin-top:6px;font-size:13px}.tableview summary{cursor:pointer;color:var(--ink2)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}
th.num{text-align:right}td.num{text-align:right}
.owners{max-width:420px;margin:8px 0 14px}
.tip{position:fixed;z-index:10;pointer-events:none;background:var(--ink);color:var(--surface);padding:6px 10px;border-radius:6px;font-size:12px;max-width:260px;display:none}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:2px 10px 2px 4px;border-radius:999px;background:var(--wash);border:1px solid var(--ring);white-space:nowrap}
.badge .ico{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;font-size:11px;color:var(--fill-ink)}
.badge.paid .ico{background:var(--good)}.badge.review .ico{background:var(--warn);color:var(--warn-ink)}.badge.rejected .ico{background:var(--crit)}
.count{font-size:13px;background:var(--wash);border:1px solid var(--ring);border-radius:999px;padding:0 8px;margin-left:6px;font-weight:600}
.owner{font-size:12px;font-weight:600;background:var(--wash);border:1px solid var(--ring);border-radius:6px;padding:1px 8px}
.searchrow{display:flex;gap:12px;align-items:center;margin:28px 0 0;flex-wrap:wrap}
.searchrow input{flex:1;min-width:200px;font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--axis);border-radius:8px;padding:6px 10px;min-height:36px}
.queue.review h2{border-left:4px solid var(--warn);padding-left:10px}.queue.paid h2{border-left:4px solid var(--good);padding-left:10px}.queue.rejected h2{border-left:4px solid var(--crit);padding-left:10px}
details.inv{background:var(--surface);border:1px solid var(--ring);border-radius:10px;margin-bottom:8px}
details.inv>summary{list-style:none;display:grid;grid-template-columns:130px 1.1fr 1.4fr 110px 1.3fr;gap:12px;align-items:center;padding:10px 14px;cursor:pointer;min-height:48px}
details.inv>summary::-webkit-details-marker{display:none}
details.inv>summary:hover{background:var(--wash)}
.c-reason{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center}
.c-id{display:flex;flex-direction:column;overflow:hidden;text-overflow:ellipsis}.c-id .muted{font-size:12px}
.card-body{padding:4px 18px 18px;border-top:1px solid var(--grid)}
.why{margin:12px 0 0}.warnline{color:var(--ink2);font-size:13px;margin:6px 0 0}.warnmark{margin-left:6px}
.path{display:flex;flex-wrap:wrap;gap:6px;list-style:none;padding:0;margin:0}
.path li{font-size:12px;background:var(--wash);border:1px solid var(--ring);border-radius:6px;padding:2px 8px}
.path li:not(:last-child)::after{content:"→";margin-left:8px;color:var(--muted)}
.reasons{list-style:none;padding:0;margin:0;display:grid;gap:6px}
.reason{display:flex;gap:10px;align-items:flex-start;font-size:13px}
.sev{flex:none;width:18px;height:18px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:var(--fill-ink);background:var(--muted)}
.reason.critical .sev{background:var(--crit)}.reason.warning .sev{background:var(--warn);color:var(--warn-ink)}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--wash);border-radius:4px;padding:1px 5px}
.turn{border-left:3px solid var(--axis);padding:2px 12px;margin:8px 0}.turn .who{font-size:12px;color:var(--ink2);font-weight:600}.turn p{margin:2px 0;font-size:13px}
.tools{margin:0;padding-left:18px;font-size:13px}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px 16px;margin:16px 0 0}
.facts dt{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.facts dd{margin:0;font-size:13px}
.email{margin-top:12px;font-size:13px}.email summary{cursor:pointer;color:var(--ink2)}
pre{white-space:pre-wrap;background:var(--wash);border-radius:8px;padding:10px 12px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.about p{margin:6px 0}
@media (max-width:720px){details.inv>summary{grid-template-columns:1fr auto;gap:4px 12px}.c-vendor,.c-reason{grid-column:1/-1}.hero-fig{font-size:44px}}
@media print{.searchrow,button{display:none}details.inv{break-inside:avoid}}
"""

JS = """
(function(){
  var root=document.documentElement;
  try{var t=localStorage.getItem('theme');if(t)root.setAttribute('data-theme',t);}catch(e){}
  var tb=document.getElementById('theme');
  if(tb)tb.addEventListener('click',function(){
    var dark=root.getAttribute('data-theme')==='dark'||(!root.getAttribute('data-theme')&&matchMedia('(prefers-color-scheme: dark)').matches);
    var next=dark?'light':'dark';root.setAttribute('data-theme',next);
    try{localStorage.setItem('theme',next);}catch(e){}
  });
  var cards=[].slice.call(document.querySelectorAll('details.inv'));
  var q=document.getElementById('search'),shown=document.getElementById('shown');
  function apply(){
    var text=(q.value||'').toLowerCase().trim(),n=0;
    cards.forEach(function(c){var ok=!text||c.dataset.search.indexOf(text)>-1;c.style.display=ok?'':'none';if(ok)n++;});
    if(shown)shown.textContent=text?n+' of '+cards.length+' match':'';
  }
  if(q)q.addEventListener('input',apply);
  var tip=document.getElementById('tip');
  document.querySelectorAll('.seg').forEach(function(s){
    function show(ev){var r=s.getBoundingClientRect();tip.textContent=s.dataset.tip;tip.style.display='block';
      var x=ev&&ev.clientX?ev.clientX:r.left+r.width/2,y=r.top;
      tip.style.left=Math.max(8,Math.min(x-tip.offsetWidth/2,innerWidth-tip.offsetWidth-8))+'px';
      tip.style.top=(y-tip.offsetHeight-8)+'px';}
    function hide(){tip.style.display='none';}
    s.addEventListener('mousemove',show);s.addEventListener('focus',function(){show();});
    s.addEventListener('mouseleave',hide);s.addEventListener('blur',hide);
  });
})();
"""


def build_report(results: list[Result], run_id: str, provider_name: str, is_mock: bool,
                 as_of: dt.date, generated: dt.datetime | None = None) -> str:
    s = summarize(results, provider_name)
    generated = generated or dt.datetime.now()
    n = s["invoices"]
    by_outcome = {o: [r for r in results if r.outcome == o] for o in Outcome}
    held, paid, rejected = (by_outcome[Outcome.NEEDS_HUMAN_REVIEW], by_outcome[Outcome.PAID],
                            by_outcome[Outcome.REJECTED])
    dupes = [r for r in results if any(x["code"] == "DUPLICATE_INVOICE" for x in r.reasons)]
    dupe_total = sum(max(r.invoice.total or 0, 0) for r in dupes)
    drafts = sum(1 for r in rejected if r.vendor_draft)

    money_parts = [
        ("paid", "Paid", s["paid_total"], _short_money(s["paid_total"])),
        ("review", "Held for review", s["review_total"], _short_money(s["review_total"])),
        ("rejected", "Rejected, not paid", s["rejected_total"], _short_money(s["rejected_total"])),
    ]
    count_parts = [
        ("paid", "Paid", s["paid"], str(s["paid"])),
        ("review", "Needs review", s["needs_review"], str(s["needs_review"])),
        ("rejected", "Rejected", s["rejected"], str(s["rejected"])),
    ]
    cost = ""
    if s["estimated_llm_cost"] is not None:
        cost = (f" Estimated model cost: ${s['estimated_llm_cost']:.4f} "
                f"(${s['cost_per_invoice']:.4f} per invoice).")
    tokens = (f"{s['llm_calls']} model calls ({s['llm_calls_per_invoice']} per invoice), "
              f"{s['input_tokens']:,} input and {s['output_tokens']:,} output tokens.{cost}")
    mock_notice = ""
    if is_mock:
        mock_notice = ('<p class="notice"><b>Offline mock run.</b> Extraction on text and PDF files is '
                       'rule-based and the approver and critic give canned answers. The structure and '
                       'rules shown here are real; the model reasoning is not.</p>')
    dup_note = (f" Includes {_plural(len(dupes), 'duplicate copy').replace('copys', 'copies')} "
                f"({money(dupe_total)}) of invoices already processed." if dupes else "")

    tiles = "".join([
        _tile("Paid", _tile_money(s["paid_total"]), _plural(s["paid"]), "paid"),
        _tile("Held for review", _tile_money(s["review_total"]), _plural(s["needs_review"]), "review"),
        _tile("Rejected, not paid", _tile_money(s["rejected_total"]), _plural(s["rejected"]), "rejected"),
        _tile("Model calls", str(s["llm_calls"]), f"{s['llm_calls_per_invoice']} per invoice"),
        _tile("Average time", f"{s['avg_seconds_per_invoice']}s", "per invoice, end to end"),
    ])
    reject_blurb = ("Blocked, with the reasons recorded. "
                    + (f"{_plural(drafts, 'invoice')} can be returned to the vendor for correction: a "
                       "draft request is attached for an AP clerk to review." if drafts else
                       "None are correctable by the vendor."))
    money_total = sum(p[2] for p in money_parts)

    body = f"""
<main>
<header class="top"><div><h1>AP queues: run {e(run_id)}</h1>
<div class="sub">{e(generated.strftime('%Y-%m-%d %H:%M'))} · LLM: {e(provider_name)}
 · past-due checks as of {e(as_of)}</div></div>
<button id="theme" type="button" aria-label="Toggle light or dark theme">Theme</button></header>
{mock_notice}
<section class="hero"><div class="hero-fig">{e(f"{s['straight_through_rate']:.0%}")}</div>
<div class="hero-cap">of {n} invoices were resolved without a person: paid, or rejected on hard evidence.
{_plural(s['needs_review'])} {'is' if s['needs_review'] == 1 else 'are'} waiting for a decision, with the
reasons attached.</div></section>
<div class="tiles">{tiles}</div>
<section class="panel"><h2 style="margin-top:0">Where the money went</h2>
{_stacked_bar('Billed amount by outcome', money_parts, money_total, 'dollars')}
{_stacked_bar('Invoices by outcome', count_parts, n, 'invoices')}
<p class="muted">Amounts are what was billed. A rejected invoice is money that was not paid.{e(dup_note)}</p></section>
<div class="searchrow"><input id="search" type="search" placeholder="Search all queues by invoice, vendor, owner or flag"
aria-label="Search all queues"><span id="shown" class="muted"></span></div>
{_queue("review", "Needs review", "Waiting for a person. Resolve with python main.py --review. Each is routed to the role best placed to decide.", held, _by_owner(held) if held else "")}
{_queue("paid", "Ready to pay", "Approved and paid. In this prototype payment is a mock call; in production this feeds the next payment run.", paid)}
{_queue("rejected", "Rejected", reject_blurb, rejected)}
<section class="panel about"><h2 style="margin-top:0">About this run</h2>
<p><b>Model use.</b> {e(tokens)}</p>
<p><b>How to read the numbers.</b> A clean invoice is approved and paid by code with no model call. The model
reads messy text, weighs invoices that have flags, and writes the notes for held and rejected invoices. The
policy floor can never be overridden by the model.</p>
<p><b>Caution.</b> If this is the provided sample set, about half the invoices are deliberately bad, so
these figures show that the controls work. They are not a savings estimate for a real invoice mix.</p>
</section>
</main><div id="tip" class="tip" role="presentation"></div>"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>AP queues report</title><style>{CSS}</style></head><body>{body}'
            f'<script>{JS}</script></body></html>')


def write_report(path: Path, results: list[Result], run_id: str, provider_name: str,
                 is_mock: bool, as_of: dt.date) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(results, run_id, provider_name, is_mock, as_of), encoding="utf-8")
    return path
