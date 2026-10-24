"""Deterministic validation. Every check is arithmetic or a database lookup,
so results are reproducible and testable. The LLM is never involved here.

Severity policy (see docs/process-notes.md):
  CRITICAL  hard fail: the invoice cannot be legitimate as billed
  WARNING   flag: passed forward as evidence for the approver
  INFO      recorded only
"""

import datetime as dt
import re
import sqlite3
from collections import defaultdict

from .config import SETTINGS, Settings
from .ledger import content_hash
from .models import Finding, Invoice, Severity
from .normalize import is_relative_date, item_key, vendor_key
from .tools import (
    committed_quantities,
    get_invoice_history,
    get_item,
    get_vendor,
    suggest_items,
)

_RELATIVE_OFFSET = {"yesterday": -1, "today": 0, "tomorrow": 1}
_IMMEDIATE_TERMS = ("immediate", "due on receipt", "upon receipt")


def _f(code, severity, message, disputable=False, item=None) -> Finding:
    return Finding(code=code, severity=severity, message=message, disputable=disputable, item=item)


def _money(x: float) -> str:
    return f"${x:,.2f}"


def check_duplicate(inv: Invoice, conn: sqlite3.Connection) -> list[Finding]:
    """Exact duplicate, revision of a prior invoice, or number collision."""
    if not inv.invoice_number:
        return []
    own_hash = content_hash(inv)
    findings = []
    for prior in get_invoice_history(conn, inv.invoice_number):
        same_vendor = vendor_key(prior["vendor"] or "") == vendor_key(inv.vendor or "")
        if prior["content_hash"] == own_hash:
            findings.append(_f(
                "DUPLICATE_INVOICE", Severity.CRITICAL,
                f"Identical to {prior['invoice_number']} already processed "
                f"({prior['status']}, from {prior['source_file']}).",
            ))
            break
        if same_vendor:
            delta = (inv.total or 0) - (prior["total"] or 0)
            findings.append(_f(
                "REVISION_OF_PRIOR", Severity.WARNING,
                f"Restates {prior['invoice_number']} (prior status {prior['status']}, "
                f"total {_money(prior['total'] or 0)}); new total {_money(inv.total or 0)}, "
                f"difference {'+' if delta >= 0 else '-'}{_money(abs(delta))}.",
            ))
        else:
            findings.append(_f(
                "INVOICE_NUMBER_COLLISION", Severity.WARNING,
                f"{inv.invoice_number} was already used by a different vendor "
                f"({prior['vendor']}).",
            ))
    return findings


def _completeness(inv: Invoice) -> list[Finding]:
    out = []
    if not inv.vendor:
        out.append(_f("MISSING_VENDOR", Severity.CRITICAL, "No vendor named on the invoice."))
    if not inv.invoice_number:
        out.append(_f("MISSING_INVOICE_NUMBER", Severity.CRITICAL,
                      "No invoice number, so it cannot be audited or checked for duplicates.",
                      disputable=True))
    if inv.total is None:
        out.append(_f("MISSING_TOTAL", Severity.CRITICAL, "No payable total found."))
    elif inv.total <= 0:
        out.append(_f("NON_POSITIVE_TOTAL", Severity.CRITICAL,
                      f"Total is {_money(inv.total)}; credit memos are a different document type."))
    if not inv.line_items:
        out.append(_f("NO_LINE_ITEMS", Severity.CRITICAL, "No line items recovered."))
    if inv.date is None:
        out.append(_f("MISSING_DATE", Severity.WARNING, "No invoice date."))
    return out


def _lines(inv: Invoice) -> list[Finding]:
    out = []
    for li in inv.line_items:
        if li.quantity < 0:
            out.append(_f("NEGATIVE_QUANTITY", Severity.CRITICAL,
                          f"{li.item}: negative quantity ({li.quantity:g}), a data error or "
                          "a disguised credit.", item=li.item))
        elif li.quantity == 0:
            out.append(_f("ZERO_QUANTITY", Severity.WARNING,
                          f"{li.item}: quantity is zero.", item=li.item))
        if li.unit_price is not None and li.unit_price < 0:
            out.append(_f("NEGATIVE_UNIT_PRICE", Severity.CRITICAL,
                          f"{li.item}: negative unit price.", item=li.item))
    return out


def _arithmetic(inv: Invoice, tol: float) -> list[Finding]:
    out = []
    line_sums = []
    for li in inv.line_items:
        computed = None
        if li.unit_price is not None:
            computed = li.quantity * li.unit_price
        if computed is not None and li.amount is not None and abs(computed - li.amount) > tol:
            out.append(_f("LINE_AMOUNT_MISMATCH", Severity.WARNING,
                          f"{li.item}: {li.quantity:g} x {_money(li.unit_price)} = "
                          f"{_money(computed)}, but the line says {_money(li.amount)}.",
                          item=li.item))
        value = li.amount if li.amount is not None else computed
        if value is not None:
            line_sums.append(value)

    computed_sub = sum(line_sums) if line_sums else None
    if inv.subtotal is not None and computed_sub is not None \
            and abs(inv.subtotal - computed_sub) > tol:
        out.append(_f("SUBTOTAL_MISMATCH", Severity.WARNING,
                      f"Stated subtotal {_money(inv.subtotal)} differs from the sum of lines "
                      f"{_money(computed_sub)}."))
    base = computed_sub if computed_sub is not None else inv.subtotal
    if inv.total is not None and base is not None:
        expected = base + (inv.tax or 0) + (inv.shipping or 0)
        if abs(expected - inv.total) > tol:
            direction = "overcharge" if inv.total > expected else "shortfall"
            out.append(_f("TOTAL_MISMATCH", Severity.WARNING,
                          f"Total {_money(inv.total)} does not equal lines + tax + shipping "
                          f"= {_money(expected)} ({direction} of "
                          f"{_money(abs(inv.total - expected))})."))
    return out


def _items_and_stock(inv: Invoice, conn: sqlite3.Connection, settings: Settings) -> list[Finding]:
    """Quantities are summed per item across all lines before the stock check,
    so splitting an item over several lines cannot hide a shortage."""
    out = []
    claimed = committed_quantities(conn) if settings.reserve_stock else {}
    demand: dict[str, list] = defaultdict(lambda: [None, 0.0, None])  # item row, qty, label
    unknown_seen = set()
    for li in inv.line_items:
        row = get_item(conn, li.item)
        if row is None:
            key = item_key(li.item)
            if key not in unknown_seen:
                unknown_seen.add(key)
                hints = suggest_items(conn, li.item)
                hint = f" Did you mean {' or '.join(hints)}? (hint only, not substituted)" if hints else ""
                out.append(_f("UNKNOWN_ITEM", Severity.CRITICAL,
                              f"'{li.item}' is not in the catalog.{hint}",
                              disputable=True, item=li.item))
            continue
        entry = demand[row["item"]]
        entry[0] = row
        entry[1] += max(li.quantity, 0)
        entry[2] = li.item

    for name, (row, qty, _) in demand.items():
        stock = row["stock"]
        if stock == 0:
            out.append(_f("OUT_OF_STOCK", Severity.CRITICAL,
                          f"{name}: billed {qty:g} but zero stock is held.", item=name))
            continue
        reserved = claimed.get(item_key(name), 0)
        if reserved and qty <= stock and qty > stock - reserved:
            out.append(_f("STOCK_CONTENTION", Severity.CRITICAL,
                          f"{name}: {stock} on hand but {reserved:g} already claimed by earlier "
                          f"paid invoices; this invoice bills {qty:g}.",
                          disputable=True, item=name))
        elif qty > stock:
            out.append(_f("INSUFFICIENT_STOCK", Severity.CRITICAL,
                          f"{name}: billed {qty:g} (summed across lines) but only {stock} "
                          "received/on hand.", disputable=True, item=name))
    return out


def _pricing(inv: Invoice, conn: sqlite3.Connection, settings: Settings) -> list[Finding]:
    """Asymmetric: overpaying is the loss. Compared only for settleable
    currencies, since comparing EUR to a USD catalog without FX is meaningless."""
    pol = settings.policy
    if inv.currency not in pol.settleable_currencies:
        return []
    out = []
    for li in inv.line_items:
        row = get_item(conn, li.item)
        if row is None or li.unit_price is None or row["unit_price"] <= 0:
            continue
        list_price, diff = row["unit_price"], li.unit_price - row["unit_price"]
        if diff > 0:
            allowed = min(list_price * pol.price_above_tolerance, pol.price_above_cap_per_unit)
            if diff > allowed:
                out.append(_f("PRICE_ABOVE_LIST", Severity.WARNING,
                              f"{li.item}: {_money(li.unit_price)} is {diff / list_price:+.0%} vs. "
                              f"catalog {_money(list_price)} (allowed up to {_money(allowed)} over)."
                              + (f" Line note: '{li.note}'." if li.note else ""),
                              item=li.item))
        elif diff < 0:
            pct = -diff / list_price
            if pct > pol.price_below_flag:
                out.append(_f("PRICE_BELOW_LIST", Severity.WARNING,
                              f"{li.item}: {_money(li.unit_price)} is {pct:.0%} below catalog "
                              f"{_money(list_price)}.", item=li.item))
            else:
                out.append(_f("PRICE_MINOR_DISCOUNT", Severity.INFO,
                              f"{li.item}: {_money(li.unit_price)} is {pct:.0%} below catalog "
                              f"{_money(list_price)}.", item=li.item))
    return out


def _vendor(inv: Invoice, conn: sqlite3.Connection, settings: Settings) -> list[Finding]:
    if not inv.vendor:
        return []
    info = get_vendor(conn, inv.vendor)
    if info["status"] == "renamed":
        return [_f("VENDOR_RENAMED", Severity.WARNING,
                   f"'{inv.vendor}' matches a former/alternate name of approved vendor "
                   f"'{info['canonical']}'. Confirm the rename and update the vendor master.")]
    if info["status"] == "approved":
        return []
    for former in inv.extras.get("former_names", []):
        if get_vendor(conn, former)["status"] in ("approved", "renamed"):
            return [_f("VENDOR_RENAMED", Severity.WARNING,
                       f"'{inv.vendor}' says it was formerly '{former}', an approved vendor. "
                       "Confirm the rename and update the vendor master.")]
    if settings.policy.require_known_vendor:
        label = "not approved" if info["status"] == "unapproved" else "not in the vendor master"
        return [_f("UNKNOWN_VENDOR", Severity.WARNING, f"Vendor '{inv.vendor}' is {label}.")]
    return []


def _dates_and_terms(inv: Invoice, settings: Settings) -> list[Finding]:
    out = []
    due = inv.due_date
    if due is None and inv.due_date_text:
        word = inv.due_date_text.strip().lower()
        if is_relative_date(word) and inv.date:
            due = inv.date + dt.timedelta(days=_RELATIVE_OFFSET[word])
            out.append(_f("RELATIVE_DUE_DATE", Severity.WARNING,
                          f"Due date written as '{word}'; resolved against the invoice date "
                          f"to {due}."))
        else:
            out.append(_f("INVALID_DUE_DATE", Severity.WARNING,
                          f"Due date '{inv.due_date_text}' could not be understood."))
    elif due is None:
        out.append(_f("MISSING_DUE_DATE", Severity.WARNING, "No due date."))

    if due and inv.date:
        if due < inv.date:
            out.append(_f("DUE_BEFORE_ISSUE", Severity.WARNING,
                          f"Due date {due} is before the invoice date {inv.date}."))
        elif due == inv.date:
            out.append(_f("DUE_ON_ISSUE", Severity.WARNING,
                          "Due on the invoice date: no time for review."))
    if due and due < settings.as_of_date:
        out.append(_f("PAST_DUE", Severity.WARNING,
                      f"Due date {due} is before the as-of date {settings.as_of_date}."))

    terms = (inv.payment_terms or "").lower()
    if any(t in terms for t in _IMMEDIATE_TERMS):
        out.append(_f("IMMEDIATE_TERMS", Severity.WARNING,
                      f"Payment terms '{inv.payment_terms}' demand payment with no review window."))
    net = re.search(r"net\s*(\d+)", terms)
    if net and due and inv.date:
        span = (due - inv.date).days
        if abs(span - int(net.group(1))) > settings.policy.terms_mismatch_days:
            out.append(_f("TERMS_MISMATCH", Severity.INFO,
                          f"Terms say Net {net.group(1)} but the due date is {span} days out."))
    for field in ("date", "due_date"):
        if f"{field}_ambiguous" in inv.extras:
            out.append(_f("AMBIGUOUS_DATE", Severity.WARNING,
                          f"{field} '{inv.extras[f'{field}_ambiguous']}' could be read as "
                          "day/month or month/day (read as US month/day)."))
    return out


def _fraud_signals(inv: Invoice, settings: Settings) -> list[Finding]:
    out = []
    pol = settings.policy
    text = " ".join(filter(None, [inv.source_text, inv.notes, inv.payment_terms])).lower()
    hits = [p for p in pol.pressure_phrases if p in text]
    if hits:
        out.append(_f("PRESSURE_LANGUAGE", Severity.WARNING,
                      "Urgency/payment-channel language: " + ", ".join(f"'{h}'" for h in hits)
                      + ". Manufactured urgency is meant to bypass review."))
    if inv.currency not in pol.settleable_currencies:
        out.append(_f("NON_SETTLEABLE_CURRENCY", Severity.WARNING,
                      f"Currency {inv.currency} cannot be settled without a treasury FX booking."))
    if inv.total:
        for threshold in (pol.scrutiny_threshold, pol.cfo_threshold):
            if threshold * (1 - pol.near_threshold_band) <= inv.total < threshold:
                out.append(_f("NEAR_THRESHOLD", Severity.WARNING,
                              f"Total {_money(inv.total)} sits just under the {_money(threshold)} "
                              "approval threshold (possible split billing)."))
    return out


def validate(
    inv: Invoice, conn: sqlite3.Connection, settings: Settings = SETTINGS
) -> list[Finding]:
    """Run every check and return all findings, most severe first."""
    findings = (
        check_duplicate(inv, conn)
        + _completeness(inv)
        + _lines(inv)
        + _arithmetic(inv, settings.policy.arithmetic_tolerance)
        + _items_and_stock(inv, conn, settings)
        + _pricing(inv, conn, settings)
        + _vendor(inv, conn, settings)
        + _dates_and_terms(inv, settings)
        + _fraud_signals(inv, settings)
    )
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    return sorted(findings, key=lambda f: order[f.severity])


def hard_fails(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == Severity.CRITICAL]


def flags(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == Severity.WARNING]
