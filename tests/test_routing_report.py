"""Routing, the notification drafts, and the HTML report."""

import json
import re

import pytest

from invoice_ap.config import INVOICE_DIR, SETTINGS
from invoice_ap.html_report import build_report, write_report
from invoice_ap.mock_llm import MockProvider
from invoice_ap.models import Outcome
from invoice_ap.notify import vendor_draft, write_notification
from invoice_ap.pipeline import process_file
from invoice_ap.routing import ROLES, suggest_owner

import main as cli


def reason(code, severity="WARNING", disputable=False, message="m"):
    return {"code": code, "severity": severity, "message": message, "disputable": disputable}


# --- routing ----------------------------------------------------------------------


def test_each_kind_of_exception_goes_to_the_role_that_can_resolve_it():
    assert suggest_owner([reason("NON_SETTLEABLE_CURRENCY")], 4125)[0] == "Treasury"
    assert suggest_owner([reason("PRICE_ABOVE_LIST")], 7185)[0] == "Buyer"
    assert suggest_owner([reason("VENDOR_RENAMED")], 9000)[0] == "Vendor management"
    assert suggest_owner([reason("POSSIBLE_REBILL")], 900)[0] == "Controller"
    assert suggest_owner([reason("REVISION_OF_PRIOR")], 5940)[0] == "AP clerk"


def test_the_most_senior_applicable_owner_wins_and_the_deciding_code_is_reported():
    owner, why = suggest_owner([reason("PRICE_ABOVE_LIST"), reason("PRESSURE_LANGUAGE")], 900)
    assert owner == "Controller" and why == ["PRESSURE_LANGUAGE"]


def test_amount_tiers_raise_the_owner():
    assert suggest_owner([reason("REVISION_OF_PRIOR")], 12_000) == ("VP", ["amount 12,000"])
    assert suggest_owner([reason("PRICE_ABOVE_LIST")], 60_000)[0] == "CFO"
    assert suggest_owner([reason("OVER_CFO_THRESHOLD")], 60_000)[0] == "CFO"


def test_unknown_codes_default_to_the_ap_clerk():
    assert suggest_owner([reason("SOMETHING_NEW")], 100)[0] == "AP clerk"
    assert set(ROLES) >= {"AP clerk", "Treasury", "Controller", "CFO"}


def test_every_held_invoice_in_a_run_has_an_owner(conn):
    results = [process_file(INVOICE_DIR / n, conn, MockProvider(), run_id="t")
               for n in ("invoice_1010.txt", "invoice_1012.pdf", "invoice_1014.xml")]
    assert [r.owner for r in results] == ["Buyer", "Controller", "Treasury"]
    assert all(r.outcome == Outcome.NEEDS_HUMAN_REVIEW for r in results)


def test_the_review_queue_shows_the_suggested_owner(conn):
    from invoice_ap import review

    process_file(INVOICE_DIR / "invoice_1014.xml", conn, MockProvider(), run_id="t")
    item = review.pending(conn)[0]
    assert item.owner == "Treasury" and item.owner_why == ["NON_SETTLEABLE_CURRENCY"]


# --- notifications ---------------------------------------------------------------


class Counting(MockProvider):
    pass


def test_a_paid_notice_is_a_template_and_costs_no_model_call(conn):
    provider = MockProvider()
    r = process_file(INVOICE_DIR / "invoice_1001.txt", conn, provider, run_id="t")
    assert r.outcome == Outcome.PAID and r.llm_calls == 0
    assert r.notification.to == "Treasury (payment run)" and "Payment reference" in r.notification.body


def test_held_and_rejected_notes_still_use_the_model_and_go_to_the_owner(conn):
    provider = MockProvider()
    held = process_file(INVOICE_DIR / "invoice_1014.xml", conn, provider, run_id="t")
    rejected = process_file(INVOICE_DIR / "invoice_1002.txt", conn, provider, run_id="t")
    assert held.notification.to == "Treasury"
    assert rejected.notification.to == "AP clerk"
    assert any(u.role == "notifier" for u in held.usage)
    assert "Suggested owner: Treasury" in held.notification.body


def test_a_vendor_correction_draft_only_for_reasons_the_vendor_can_fix(conn):
    p = MockProvider()
    fixable = process_file(INVOICE_DIR / "invoice_1002.txt", conn, p, run_id="t")   # stock shortfall
    fraudy = process_file(INVOICE_DIR / "invoice_1003.txt", conn, p, run_id="t")    # no stock + pressure
    unknown = process_file(INVOICE_DIR / "invoice_1008.txt", conn, p, run_id="t")   # unknown vendor
    negative = process_file(INVOICE_DIR / "invoice_1009.json", conn, p, run_id="t")
    assert fixable.vendor_draft and fixable.vendor_draft.to == "Gadgets Co."
    assert "INV-1002" in fixable.vendor_draft.body and "DRAFT for AP review" in fixable.vendor_draft.body
    assert not fraudy.vendor_draft      # an item with no stock at all is not the vendor's to fix
    assert not unknown.vendor_draft     # unknown vendor: contacting the sender is a person's call
    assert not negative.vendor_draft    # no vendor to write to, and not correctable


def test_no_vendor_draft_for_duplicates_or_non_rejections():
    dup = {"outcome": "REJECTED", "vendor": "V", "invoice_number": "X-1", "total": 5,
           "reasons": [reason("DUPLICATE_INVOICE", "CRITICAL", False)]}
    assert vendor_draft(dup) is None
    held = {"outcome": "NEEDS_HUMAN_REVIEW", "vendor": "V", "reasons": []}
    assert vendor_draft(held) is None


def test_the_hint_marker_is_removed_from_vendor_text():
    facts = {"outcome": "REJECTED", "vendor": "V", "invoice_number": "X-1", "total": 5,
             "reasons": [reason("UNKNOWN_ITEM", "CRITICAL", True,
                                "'WidgetD' is not in the catalog. Did you mean WidgetA? (hint only, not substituted)")]}
    assert "hint only" not in vendor_draft(facts).body


def test_drafts_are_written_to_files_with_a_recipient(conn, tmp_path):
    process_file(INVOICE_DIR / "invoice_1002.txt", conn, MockProvider(), run_id="t", out_dir=tmp_path)
    note = (tmp_path / "notifications" / "invoice_1002.txt").read_text()
    vendor = (tmp_path / "notifications" / "invoice_1002.vendor.txt").read_text()
    assert note.startswith("To: AP clerk") and vendor.startswith("To: Gadgets Co.")


# --- the HTML report -------------------------------------------------------------


def _run(conn, names, provider=None):
    provider = provider or MockProvider()
    return [process_file(INVOICE_DIR / n, conn, provider, run_id="rep") for n in names]


@pytest.fixture(scope="module")
def sample_run():
    from invoice_ap.db import init_db

    conn = init_db(":memory:")
    names = ["invoice_1001.txt", "invoice_1002.txt", "invoice_1003.txt", "invoice_1004.json",
             "invoice_1004_revised.json", "invoice_1008.txt", "invoice_1009.json", "invoice_1010.txt",
             "invoice_1011.pdf", "invoice_1011.txt", "invoice_1014.xml", "invoice_1016.json"]
    return _run(conn, names)


def test_the_report_has_the_queues_the_groups_and_the_numbers(sample_run):
    html = build_report(sample_run, "rep", "mock", True, SETTINGS.as_of_date)
    for text in ("Invoice run", "to review", "Needs review", "Ready to pay", "Rejected",
                 "Blocked: possible fraud", "Fixable by vendor", "Duplicates", "Invalid, not correctable",
                 "Offline mock run: model reasoning is canned", "Draft: correction request to vendor",
                 "Suggested owner", "Possible fraud."):
        assert text in html, text


def test_rejections_are_grouped_by_what_can_be_done_about_them(sample_run):
    from invoice_ap.html_report import _view

    groups = {r.file: _view(i, r).group for i, r in enumerate(sample_run)
              if r.outcome == Outcome.REJECTED}
    assert groups["invoice_1002.txt"] == "fix"        # stock shortfall: the vendor can correct it
    assert groups["invoice_1003.txt"] == "fraud"      # pressure language and an unknown vendor
    assert groups["invoice_1008.txt"] == "fraud"      # unknown vendor
    assert groups["invoice_1009.json"] == "other"     # negative totals, no vendor to write to
    assert groups["invoice_1011.txt"] == "dup"


def test_the_report_is_self_contained(sample_run):
    html = build_report(sample_run, "rep", "mock", True, SETTINGS.as_of_date)
    assert not re.search(r"https?://", html)                          # no network, no CDN
    assert not re.search(r"<(link|img|iframe|object|embed)\b", html)  # no external resources
    assert html.count("<script>") == 1 and "src=" not in html


def test_every_invoice_has_a_detail_template_and_a_queue_row(sample_run):
    html = build_report(sample_run, "rep", "mock", True, SETTINGS.as_of_date)
    assert html.count('<template id="d-i') == len(sample_run)
    assert html.count('class="ap-row-item"') == len(sample_run)


def test_everything_from_an_invoice_is_escaped(conn, tmp_path):
    evil = tmp_path / "evil.json"
    payload = "<img src=x onerror=alert(1)>"
    evil.write_text(json.dumps({
        "invoice_number": "INV-6666", "vendor": {"name": payload}, "date": "2026-01-20",
        "due_date": "2026-02-20", "currency": "USD", "payment_terms": "Net 30",
        "line_items": [{"item": "<script>alert(2)</script>", "quantity": 1, "unit_price": 5.0,
                        "note": "</template><b>x</b>"}],
        "subtotal": 5.0, "tax_amount": 0.0, "total": 5.0, "notes": "<script>alert(3)</script>"}))
    result = process_file(evil, conn, MockProvider(), run_id="x")
    html = build_report([result], "x", "mock", True, SETTINGS.as_of_date)
    for raw in (payload, "<script>alert(2)", "<script>alert(3)", "</template><b>x</b>"):
        assert raw not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert html.count("<script>") == 1  # the report's own script, and nothing injected


def test_the_report_marks_operational_warnings_and_names_a_live_provider(conn):
    result = process_file(INVOICE_DIR / "invoice_1001.txt", conn, MockProvider(), run_id="x")
    result.warnings.append("approval model unavailable (HTTP 403)")
    html = build_report([result], "x", "grok", False, SETTINGS.as_of_date)
    assert "approval model unavailable" in html and "Offline mock run" not in html
    assert "Provider grok" in html


def test_a_run_with_nothing_held_says_so(conn):
    html = build_report(_run(conn, ["invoice_1001.txt"]), "x", "mock", True, SETTINGS.as_of_date)
    assert "Nothing to review" in html and "Nothing is waiting for a person" in html
    assert "No fraud flags in this run." in html


def test_a_large_run_builds_quickly_and_stays_a_reasonable_size(conn, tmp_path):
    import time

    for i in range(150):
        (tmp_path / f"inv{i}.json").write_text(json.dumps({
            "invoice_number": f"INV-{7000 + i}", "vendor": {"name": f"Brand New Vendor {i}"},
            "date": "2026-01-20", "due_date": "2026-02-20", "currency": "USD", "payment_terms": "Net 30",
            "line_items": [{"item": "SteelBracket", "quantity": 10 + i % 7, "unit_price": 45.0}],
            "subtotal": 45.0 * (10 + i % 7), "tax_amount": 0.0, "total": 45.0 * (10 + i % 7)}))
    results = [process_file(p, conn, MockProvider(), run_id="big") for p in sorted(tmp_path.glob("*.json"))]
    started = time.perf_counter()
    html = build_report(results, "big", "mock", True, SETTINGS.as_of_date)
    assert time.perf_counter() - started < 2.0
    assert len(html) < 8_000_000
    held_rows = re.findall(r'<li class="ap-row-item" data-key="i\d+" data-owner="Vendor management"', html)
    assert len(held_rows) == 150  # all held: unknown vendors
    assert "data-more" in html  # the "Show N more" control exists for lists over 12


def test_write_report_creates_the_file(sample_run, tmp_path):
    path = write_report(tmp_path / "deep" / "report.html", sample_run, "rep", "mock", True, SETTINGS.as_of_date)
    assert path.exists() and path.read_text().startswith("<!doctype html>")


def test_cli_writes_the_report_and_can_skip_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "RUN_DIR", tmp_path / "runs")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    db = str(tmp_path / "t.db")
    cli.main([f"--invoice_path={INVOICE_DIR / 'invoice_1001.txt'}", "--fresh", "--brief", "--db", db])
    assert len(list((tmp_path / "runs").glob("*/report.html"))) == 1
    assert "AP queues report" in capsys.readouterr().out
    cli.main([f"--invoice_path={INVOICE_DIR / 'invoice_1006.csv'}", "--no-report", "--brief", "--db", db])
    assert len(list((tmp_path / "runs").glob("*/report.html"))) == 1  # still just the first run's
