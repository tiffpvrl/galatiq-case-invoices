import datetime as dt

import pytest

from invoice_ap.config import INVOICE_DIR
from invoice_ap.readers import ReadError, read_invoice


def load(name):
    return read_invoice(INVOICE_DIR / name)


def lines(inv):
    return [(li.item, li.quantity, li.unit_price) for li in inv.line_items]


def test_all_sample_files_are_readable():
    for path in sorted(INVOICE_DIR.iterdir()):
        inv = read_invoice(path)
        assert inv.invoice_number, path.name
        assert inv.line_items, path.name


@pytest.mark.parametrize("stem", ["invoice_1011", "invoice_1012", "invoice_1013"])
def test_pdf_and_txt_or_json_extract_the_same_content(stem):
    pdf = load(f"{stem}.pdf")
    other = load(f"{stem}.json" if stem.endswith("1013") else f"{stem}.txt")
    assert pdf.invoice_number == other.invoice_number
    assert pdf.vendor == other.vendor
    assert pdf.total == other.total
    assert lines(pdf) == lines(other)


def test_messy_text_with_typos():
    inv = load("invoice_1002.txt")  # "INVOCE", "Vndr", "Inv #: 1002"
    assert inv.invoice_number == "INV-1002"
    assert inv.vendor == "Gadgets Co."
    assert lines(inv) == [("GadgetX", 20, 750.0)]
    assert inv.total == 15000.0


def test_relative_due_date_is_kept_as_text_not_guessed():
    inv = load("invoice_1003.txt")
    assert inv.due_date is None
    assert inv.due_date_text == "yesterday"


def test_ocr_confusables_in_text_invoice():
    inv = load("invoice_1012.txt")  # "26-Jan-2O26", "$3,500.O0"
    assert inv.date == dt.date(2026, 1, 26)
    assert inv.total == 9975.0
    assert [li.amount for li in inv.line_items] == [3000.0, 3500.0, 3000.0]
    assert inv.vendor == "QuickShip Distributers"
    assert inv.extras["former_names"] == ["FastShip Ltd."]


def test_email_style_invoice():
    inv = load("invoice_1008.txt")
    assert inv.vendor == "NoProd Industries"
    assert lines(inv) == [("SuperGizmo", 12, 400.0), ("MegaSprocket", 6, 850.0)]


def test_item_note_split_from_name():
    inv = load("invoice_1010.txt")
    rush = inv.line_items[-1]
    assert (rush.item, rush.unit_price, rush.note) == ("WidgetA", 300.0, "rush order")
    assert inv.shipping == 150.0 and inv.total == 7185.0


def test_absent_fields_stay_none_not_invented():
    inv = load("invoice_1009.json")
    assert inv.vendor is None
    assert inv.due_date is None
    assert inv.total == -250.0
    assert inv.line_items[0].quantity == -5


def test_csv_key_value_and_table_layouts():
    kv = load("invoice_1006.csv")
    assert kv.total == 2750.0 and len(kv.line_items) == 2
    table = load("invoice_1007.csv")
    assert table.date == dt.date(2026, 1, 28)  # 01/28/2026, US order
    assert table.tax == 885.0 and table.total == 15525.0
    assert len(table.line_items) == 3


def test_xml_currency_and_totals():
    inv = load("invoice_1014.xml")
    assert inv.currency == "EUR"
    assert inv.total == 4125.0 and inv.tax == 375.0


def test_revision_marker_is_read():
    assert load("invoice_1004_revised.json").revision == "R1"
    assert load("invoice_1004.json").revision is None


def test_malformed_and_unsupported_inputs_raise_read_error(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json")
    with pytest.raises(ReadError):
        read_invoice(bad_json)
    bad_xml = tmp_path / "bad.xml"
    bad_xml.write_text("<invoice><header>")
    with pytest.raises(ReadError):
        read_invoice(bad_xml)
    with pytest.raises(ReadError):
        read_invoice(tmp_path / "missing.json")
    other = tmp_path / "invoice.docx"
    other.write_text("x")
    with pytest.raises(ReadError):
        read_invoice(other)


def test_xml_entity_expansion_is_rejected(tmp_path):
    bomb = tmp_path / "bomb.xml"
    bomb.write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        "<invoice><header><invoice_number>&b;</invoice_number></header></invoice>"
    )
    with pytest.raises(ReadError):
        read_invoice(bomb)
