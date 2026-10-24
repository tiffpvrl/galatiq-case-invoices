from invoice_ap.config import INVOICE_DIR
from invoice_ap.mock_llm import MockProvider
from invoice_ap.models import Outcome
from invoice_ap.payment import pay
from invoice_ap.pipeline import process_file

P, R, H = Outcome.PAID, Outcome.REJECTED, Outcome.NEEDS_HUMAN_REVIEW

# Processed in this order against a fresh ledger (so duplicates and revisions
# resolve deterministically). Outcomes with the offline mock provider.
EXPECTED = [
    ("invoice_1001.txt", P),
    ("invoice_1002.txt", R),   # 20 GadgetX billed, 5 on hand
    ("invoice_1003.txt", R),   # FakeItem, zero stock
    ("invoice_1004.json", P),
    ("invoice_1004_revised.json", H),  # revision of a paid invoice
    ("invoice_1005.json", R),  # 8 GadgetX billed, 5 on hand
    ("invoice_1006.csv", P),
    ("invoice_1007.csv", R),   # stock shortfall on two items
    ("invoice_1008.txt", R),   # unknown items
    ("invoice_1009.json", R),  # negative quantity and total, no vendor
    ("invoice_1010.txt", H),   # rush premium is only the sender's claim: unverified
    ("invoice_1011.txt", P),
    ("invoice_1011.pdf", R),   # duplicate of the txt
    ("invoice_1012.txt", H),   # vendor renamed
    ("invoice_1012.pdf", R),   # duplicate
    ("invoice_1013.json", R),  # stock summed across lines
    ("invoice_1013.pdf", R),   # duplicate / stock
    ("invoice_1014.xml", H),   # EUR
    ("invoice_1015.csv", P),
    ("invoice_1016.json", R),  # unknown item
]


def run_all(conn, provider=None, **kw):
    provider = provider or MockProvider()
    return {name: process_file(INVOICE_DIR / name, conn, provider, run_id="t", **kw)
            for name, _ in EXPECTED}


def test_every_sample_reaches_the_expected_outcome(conn):
    results = run_all(conn)
    for name, expected in EXPECTED:
        assert results[name].outcome == expected, (name, results[name].path)


def test_every_sample_file_is_covered():
    assert {n for n, _ in EXPECTED} | {"invoice_1004_revised.json"} >= {
        p.name for p in INVOICE_DIR.iterdir()}


def test_total_paid_matches_the_paid_invoices(conn):
    results = run_all(conn)
    paid = sum(r.invoice.total for r in results.values() if r.outcome == P)
    assert paid == 5000 + 1890 + 2750 + 3000 + 6500


def test_no_invoice_with_a_hard_fail_is_ever_paid(conn):
    for r in run_all(conn).values():
        if any(f.severity == "CRITICAL" for f in r.findings):
            assert r.outcome == R and r.payment is None


def test_clean_invoice_makes_no_llm_calls_except_the_notification(conn):
    r = process_file(INVOICE_DIR / "invoice_1001.txt", conn, MockProvider(), run_id="t")
    assert r.path[-2:] == ["auto-approve", "pay"]
    assert [u.role for u in r.usage] == [] or all(u.role == "notifier" for u in r.usage)


def test_floor_escalation_skips_the_approver(conn):
    r = process_file(INVOICE_DIR / "invoice_1014.xml", conn, MockProvider(), run_id="t")
    assert r.outcome == H and r.approval is None


def test_approver_runs_and_uses_tools_for_a_judgment_case(conn):
    r = process_file(INVOICE_DIR / "invoice_1010.txt", conn, MockProvider(), run_id="t")
    assert r.approval is not None
    assert any(c["tool"] == "get_catalog_price" for c in r.approval.tool_calls)


def test_rerunning_an_invoice_does_not_pay_twice(conn):
    first = process_file(INVOICE_DIR / "invoice_1001.txt", conn, MockProvider(), run_id="a")
    again = process_file(INVOICE_DIR / "invoice_1001.txt", conn, MockProvider(), run_id="b")
    assert first.outcome == P and again.outcome == R  # caught as a duplicate
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_payment_is_idempotent_at_the_gateway(conn):
    calls = []
    gateway = lambda v, a: calls.append((v, a)) or {"status": "success"}
    pay(conn, "k1", "Acme", 10.0, gateway)
    replay = pay(conn, "k1", "Acme", 10.0, gateway)
    assert len(calls) == 1 and replay.replayed


def test_payment_failure_goes_to_human_review_and_is_not_recorded_as_paid(conn):
    def boom(vendor, amount):
        raise ConnectionError("bank down")

    r = process_file(INVOICE_DIR / "invoice_1001.txt", conn, MockProvider(), run_id="t", gateway=boom)
    assert r.outcome == H and r.payment.status == "failed"
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


def test_unexpected_error_never_pays(conn, monkeypatch):
    import invoice_ap.pipeline as pipeline

    monkeypatch.setattr(pipeline, "validate", lambda *a, **k: 1 / 0)
    r = process_file(INVOICE_DIR / "invoice_1001.txt", conn, MockProvider(), run_id="t")
    assert r.outcome == H and r.payment is None
    assert any(f.code == "INTERNAL_ERROR" for f in r.findings)


def test_unreadable_file_goes_to_human_review(conn, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    r = process_file(bad, conn, MockProvider(), run_id="t")
    assert r.outcome == H
    assert any(f.code == "EXTRACTION_FAILED" for f in r.findings)


def test_every_outcome_is_recorded_in_the_ledger_and_notified(conn, tmp_path):
    run_all(conn, out_dir=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM invoices_processed").fetchone()[0] == len(EXPECTED)
    assert len(list((tmp_path / "notifications").glob("*.txt"))) >= 15
    assert (tmp_path / "trace.jsonl").read_text().strip()
