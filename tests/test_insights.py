import json

import pytest
from rich.console import Console

from invoice_ap import review
from invoice_ap.config import INVOICE_DIR
from invoice_ap.insights import compute_insights, render, wilson_lower
from invoice_ap.mock_llm import MockProvider
from invoice_ap.pipeline import process_file

import main as cli


def add_decision(conn, decision, codes, reviewer="alice", n=[0]):
    """Insert an escalated invoice carrying `codes` and a human decision on it."""
    n[0] += 1
    reasons = [{"code": c, "severity": "WARNING", "message": "m", "disputable": False} for c in codes]
    cur = conn.execute(
        "INSERT INTO invoices_processed (run_id, invoice_number, vendor, content_hash, total, "
        "currency, status, reasons) VALUES ('t', ?, 'V', ?, 100, 'USD', 'NEEDS_HUMAN_REVIEW', ?)",
        (f"X-{n[0]}", f"h{n[0]}", json.dumps(reasons)))
    conn.execute("INSERT INTO review_decisions (ledger_id, decision, reviewer) VALUES (?, ?, ?)",
                 (cur.lastrowid, decision, reviewer))
    conn.commit()


def fill(conn, code, approve=0, reject=0, **kw):
    for _ in range(approve):
        add_decision(conn, "approve", [code], **kw)
    for _ in range(reject):
        add_decision(conn, "reject", [code], **kw)


def stats(conn, code):
    return next(s for s in compute_insights(conn).codes if s.code == code)


def test_wilson_lower_bound_is_conservative_with_small_samples():
    assert wilson_lower(0, 0) == 0
    assert wilson_lower(5, 5) < 0.6          # 5 of 5 is not enough evidence
    assert wilson_lower(12, 12) > 0.75       # 12 of 12 is
    assert wilson_lower(12, 12) > wilson_lower(6, 6)
    assert wilson_lower(6, 12) < 0.5


def test_a_forced_flag_that_reviewers_always_approve_is_suggested_for_relaxing(conn):
    fill(conn, "NON_SETTLEABLE_CURRENCY", approve=12)
    s = stats(conn, "NON_SETTLEABLE_CURRENCY")
    assert s.verdict == "relax" and "FORCE_ESCALATE" in s.suggestion


def test_a_flag_that_reviewers_always_reject_is_suggested_for_hardening(conn):
    fill(conn, "PRESSURE_LANGUAGE", reject=12)
    s = stats(conn, "PRESSURE_LANGUAGE")
    assert s.verdict == "harden" and "CRITICAL" in s.suggestion


def test_five_of_five_is_not_enough_to_change_policy(conn):
    fill(conn, "NON_SETTLEABLE_CURRENCY", approve=5)
    s = stats(conn, "NON_SETTLEABLE_CURRENCY")
    assert s.verdict == "keep" and s.suggestion is None


def test_split_decisions_keep_the_flag(conn):
    fill(conn, "UNKNOWN_VENDOR", approve=7, reject=6)
    assert stats(conn, "UNKNOWN_VENDOR").verdict == "keep"


def test_too_few_decisions_is_reported_as_insufficient_data(conn):
    fill(conn, "REVISION_OF_PRIOR", approve=3)
    assert stats(conn, "REVISION_OF_PRIOR").verdict == "too few"


def test_multi_flag_invoices_do_not_count_toward_a_single_flags_evidence(conn):
    fill(conn, "VENDOR_RENAMED", approve=12)
    for _ in range(20):  # twenty rejections, but each carried a second flag
        add_decision(conn, "reject", ["VENDOR_RENAMED", "NEAR_THRESHOLD"])
    s = stats(conn, "VENDOR_RENAMED")
    assert (s.solo_approved, s.solo_rejected) == (12, 0)  # attribution stays clean
    assert s.rejected == 20 and s.verdict == "relax"


def test_holds_are_recorded_but_do_not_count_as_decisions(conn):
    fill(conn, "UNKNOWN_VENDOR", approve=2)
    add_decision(conn, "hold", ["UNKNOWN_VENDOR"])
    report = compute_insights(conn)
    assert report.decisions == 2 and report.holds == 1


def test_a_flag_the_approver_escalates_is_attributed_to_the_approver(conn):
    fill(conn, "PRICE_ABOVE_LIST", approve=12)
    s = stats(conn, "PRICE_ABOVE_LIST")
    assert s.handling == "approver" and "approver" in s.suggestion


def test_simulated_reviewers_are_labelled_in_the_output(conn, capsys):
    fill(conn, "UNKNOWN_VENDOR", approve=1, reviewer="simulated")
    console = Console(record=True, width=120)
    render(console, compute_insights(conn))
    assert "SIMULATED" in console.export_text()


def test_no_decisions_explains_how_to_get_some(conn):
    console = Console(record=True, width=120)
    render(console, compute_insights(conn))
    assert "--review" in console.export_text()


def test_real_review_decisions_flow_into_insights(conn):
    process_file(INVOICE_DIR / "invoice_1014.xml", conn, MockProvider(), run_id="t")  # EUR: escalated
    item = review.pending(conn)[0]
    review.resolve(conn, item.ledger_id, "approve", "tiff", "FX booked")
    s = stats(conn, "NON_SETTLEABLE_CURRENCY")
    assert (s.approved, s.rejected, s.solo_approved) == (1, 0, 1) and s.verdict == "too few"


def test_cli_insights_json_and_empty_database(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    db = str(tmp_path / "t.db")
    assert cli.main(["--insights", "--json", "--fresh", "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["decisions"] == 0
