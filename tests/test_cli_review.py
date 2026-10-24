import json

import pytest

from invoice_ap import review
from invoice_ap.config import INVOICE_DIR
from invoice_ap.db import init_db
from invoice_ap.mock_llm import MockProvider
from invoice_ap.models import Outcome
from invoice_ap.pipeline import process_file
from invoice_ap.report import money, summarize

import main as cli


def run(conn, name, **kw):
    return process_file(INVOICE_DIR / name, conn, MockProvider(), run_id="t", **kw)


# --- review queue -----------------------------------------------------------------


def test_pending_lists_only_escalated_invoices(conn):
    run(conn, "invoice_1001.txt")  # paid
    run(conn, "invoice_1002.txt")  # rejected
    run(conn, "invoice_1014.xml")  # EUR: escalated
    assert [p.invoice_number for p in review.pending(conn)] == ["INV-1014"]


def test_approving_pays_once_and_closes_the_item(conn):
    run(conn, "invoice_1014.xml")
    item = review.pending(conn)[0]
    payment = review.resolve(conn, item.ledger_id, "approve", "tiff", "FX booked by treasury")
    assert payment.status == "success"
    assert review.pending(conn) == []
    status = conn.execute("SELECT status FROM invoices_processed WHERE id = ?",
                          (item.ledger_id,)).fetchone()[0]
    assert status == Outcome.PAID.value
    with pytest.raises(review.ReviewError):  # cannot decide twice
        review.resolve(conn, item.ledger_id, "approve", "tiff")


def test_rejecting_closes_without_paying(conn):
    run(conn, "invoice_1014.xml")
    item = review.pending(conn)[0]
    assert review.resolve(conn, item.ledger_id, "reject", "tiff") is None
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    assert review.pending(conn) == []


def test_hold_keeps_the_item_pending_and_is_recorded(conn):
    run(conn, "invoice_1014.xml")
    item = review.pending(conn)[0]
    review.resolve(conn, item.ledger_id, "hold", "tiff", "waiting on treasury")
    assert len(review.pending(conn)) == 1
    assert conn.execute("SELECT note FROM review_decisions").fetchone()[0] == "waiting on treasury"


def test_approving_a_revision_pays_only_the_difference(conn):
    run(conn, "invoice_1004.json")          # paid $1,890
    run(conn, "invoice_1004_revised.json")  # escalated: revision, $5,940
    item = review.pending(conn)[0]
    review.resolve(conn, item.ledger_id, "approve", "tiff")
    paid = {r["amount"] for r in conn.execute("SELECT amount FROM payments")}
    assert paid == {1890.0, 4050.0}  # never the full $5,940 on top of the original


def test_an_incomplete_invoice_cannot_be_approved_for_payment(conn, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    process_file(bad, conn, MockProvider(), run_id="t")
    item = review.pending(conn)[0]
    with pytest.raises(review.ReviewError, match="incomplete"):
        review.resolve(conn, item.ledger_id, "approve", "tiff")


def test_failed_payment_leaves_the_invoice_pending(conn):
    run(conn, "invoice_1014.xml")
    item = review.pending(conn)[0]

    def down(vendor, amount):
        raise ConnectionError("bank down")

    with pytest.raises(review.ReviewError, match="left pending"):
        review.resolve(conn, item.ledger_id, "approve", "tiff", gateway=down)
    assert len(review.pending(conn)) == 1


def test_invalid_decision_is_rejected(conn):
    with pytest.raises(review.ReviewError):
        review.resolve(conn, 1, "maybe", "tiff")


# --- summary metrics --------------------------------------------------------------


def test_summary_metrics(conn):
    results = [run(conn, n) for n in
               ("invoice_1001.txt", "invoice_1002.txt", "invoice_1014.xml", "invoice_1009.json")]
    s = summarize(results, "mock")
    assert (s["paid"], s["rejected"], s["needs_review"]) == (1, 2, 1)
    assert s["paid_total"] == 5000.0
    assert s["rejected_total"] == 15000.0  # the -$250 invoice does not reduce it
    assert s["straight_through_rate"] == 0.75 and s["escalation_rate"] == 0.25


def test_money_formats_negatives():
    assert money(-250) == "-$250.00" and money(1234.5) == "$1,234.50" and money(None) == "n/a"


# --- CLI ---------------------------------------------------------------------------


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "RUN_DIR", tmp_path / "runs")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    return str(tmp_path / "t.db")


def test_cli_single_invoice_json(cli_db, capsys):
    code = cli.main([f"--invoice_path={INVOICE_DIR / 'invoice_1001.txt'}", "--fresh",
                     "--json", "--db", cli_db])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["invoices"][0]["outcome"] == "PAID"
    assert out["summary"]["paid_total"] == 5000.0


def test_cli_all_processes_every_sample_file(cli_db, capsys):
    cli.main(["--all", "--fresh", "--json", "--db", cli_db])
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["invoices"] == len(list(INVOICE_DIR.iterdir()))
    assert out["summary"]["paid"] == 5


def test_cli_without_a_source_prints_usage_and_exits_2(cli_db, capsys):
    assert cli.main(["--db", cli_db]) == 2


def test_cli_missing_file_is_a_clean_error(cli_db):
    with pytest.raises(SystemExit, match="not found"):
        cli.main(["--invoice_path=nope.txt", "--db", cli_db])


def test_cli_directory_input_and_state_persists_between_runs(cli_db, capsys, tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.txt").write_text((INVOICE_DIR / "invoice_1001.txt").read_text())
    (folder / "ignore.md").write_text("not an invoice")
    cli.main([f"--invoice_path={folder}", "--fresh", "--json", "--db", cli_db])
    first = json.loads(capsys.readouterr().out)
    assert [i["outcome"] for i in first["invoices"]] == ["PAID"]
    cli.main([f"--invoice_path={folder}", "--json", "--db", cli_db])  # no --fresh
    again = json.loads(capsys.readouterr().out)
    assert again["invoices"][0]["outcome"] == "REJECTED"  # caught as a duplicate
    assert "DUPLICATE_INVOICE" in {r["code"] for r in again["invoices"][0]["reasons"]}


def test_cli_review_with_nothing_pending(cli_db, capsys):
    assert cli.main(["--review", "--fresh", "--db", cli_db]) == 0
