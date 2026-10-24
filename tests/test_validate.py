import dataclasses

import pytest

from invoice_ap.config import SETTINGS
from invoice_ap.ledger import record
from invoice_ap.models import LineItem, Outcome, Severity
from invoice_ap.validate import check_duplicate, flags, hard_fails, validate

# Expected (hard-fail codes, flag codes) per sample file against the seed data.
# A hard fail is a CRITICAL finding; flags are WARNINGs.
GOLDEN = {
    "invoice_1001.txt": ({}, set()),
    "invoice_1002.txt": ({"INSUFFICIENT_STOCK"}, {"DUE_ON_ISSUE", "PAST_DUE"}),
    "invoice_1003.txt": (
        {"OUT_OF_STOCK"},
        {"UNKNOWN_VENDOR", "RELATIVE_DUE_DATE", "DUE_BEFORE_ISSUE", "PAST_DUE",
         "IMMEDIATE_TERMS", "PRESSURE_LANGUAGE"},
    ),
    "invoice_1004.json": ({}, set()),
    "invoice_1004_revised.json": ({}, set()),  # revision flag needs the original in the ledger
    "invoice_1005.json": ({"INSUFFICIENT_STOCK"}, set()),
    "invoice_1006.csv": ({}, set()),
    "invoice_1007.csv": ({"INSUFFICIENT_STOCK"}, {"TOTAL_MISMATCH"}),
    "invoice_1008.txt": ({"UNKNOWN_ITEM"}, {"UNKNOWN_VENDOR", "PAST_DUE", "NEAR_THRESHOLD"}),
    "invoice_1009.json": (
        {"MISSING_VENDOR", "NON_POSITIVE_TOTAL", "NEGATIVE_QUANTITY"},
        {"SUBTOTAL_MISMATCH", "MISSING_DUE_DATE"},
    ),
    "invoice_1010.txt": ({}, {"PRICE_ABOVE_LIST"}),
    "invoice_1011.txt": ({}, set()),
    "invoice_1011.pdf": ({}, set()),
    "invoice_1012.txt": ({}, {"VENDOR_RENAMED", "NEAR_THRESHOLD"}),
    "invoice_1012.pdf": ({}, {"VENDOR_RENAMED", "NEAR_THRESHOLD"}),
    "invoice_1013.json": ({"INSUFFICIENT_STOCK"}, {"TOTAL_MISMATCH"}),
    "invoice_1013.pdf": ({"INSUFFICIENT_STOCK"}, {"TOTAL_MISMATCH"}),
    "invoice_1014.xml": ({}, {"NON_SETTLEABLE_CURRENCY"}),
    "invoice_1015.csv": ({}, set()),
    "invoice_1016.json": ({"UNKNOWN_ITEM"}, set()),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_sample_invoice_findings(name, conn, load):
    findings = validate(load(name), conn)
    expected_hard, expected_flags = GOLDEN[name]
    assert {f.code for f in hard_fails(findings)} == set(expected_hard), name
    assert {f.code for f in flags(findings)} == expected_flags, name


def test_every_sample_file_has_a_golden_entry():
    from invoice_ap.config import INVOICE_DIR

    assert {p.name for p in INVOICE_DIR.iterdir()} == set(GOLDEN)


def test_clean_synthetic_invoice_has_no_findings(conn, make):
    assert validate(make(), conn) == []


def test_stock_is_summed_across_lines(conn, make):
    # 8 + 8 = 16 > 15 in stock, though each line alone fits (INV-1013's pattern).
    inv = make(line_items=[
        LineItem(item="WidgetA", quantity=8, unit_price=250.0, amount=2000.0),
        LineItem(item="WidgetA", quantity=8, unit_price=250.0, amount=2000.0),
    ], subtotal=4000.0, total=4000.0)
    codes = {f.code for f in validate(inv, conn)}
    assert "INSUFFICIENT_STOCK" in codes


def test_item_name_variants_match_but_neighbours_do_not(conn, make):
    ok = make(line_items=[LineItem(item="Widget A", quantity=1, unit_price=250.0, amount=250.0)],
              subtotal=250.0, total=250.0)
    assert not any(f.code == "UNKNOWN_ITEM" for f in validate(ok, conn))
    near = make(line_items=[LineItem(item="WidgetD", quantity=1, unit_price=250.0, amount=250.0)],
                subtotal=250.0, total=250.0)
    unknown = [f for f in validate(near, conn) if f.code == "UNKNOWN_ITEM"]
    assert unknown and "hint only" in unknown[0].message  # suggests, never substitutes


@pytest.mark.parametrize(
    "price, expect",
    [
        (252.5, None),  # +1%: within tolerance
        (260.0, "PRICE_ABOVE_LIST"),  # +4%: over 2%
        (240.0, "PRICE_MINOR_DISCOUNT"),  # -4%: INFO only
        (200.0, "PRICE_BELOW_LIST"),  # -20%
    ],
)
def test_asymmetric_price_tolerance(price, expect, conn, make):
    inv = make(line_items=[LineItem(item="WidgetA", quantity=1, unit_price=price, amount=price)],
               subtotal=price, total=price)
    codes = {f.code for f in validate(inv, conn) if f.code.startswith("PRICE")}
    assert codes == ({expect} if expect else set())


def test_price_dollar_cap_governs_on_expensive_items(conn, make):
    # ControlPanel lists at $2,500: 2% would allow $50, but the $25 cap governs.
    inv = make(line_items=[LineItem(item="ControlPanel", quantity=1, unit_price=2530.0,
                                    amount=2530.0)], subtotal=2530.0, total=2530.0)
    assert "PRICE_ABOVE_LIST" in {f.code for f in validate(inv, conn)}


def test_foreign_currency_skips_price_comparison_and_flags(conn, load):
    codes = {f.code for f in validate(load("invoice_1014.xml"), conn)}
    assert "NON_SETTLEABLE_CURRENCY" in codes
    assert not any(c.startswith("PRICE") for c in codes)


def test_past_due_uses_the_configured_as_of_date_not_the_wall_clock(conn, make):
    inv = make(due_date="2026-02-14")
    assert "PAST_DUE" not in {f.code for f in validate(inv, conn)}
    later = dataclasses.replace(SETTINGS, as_of_date=__import__("datetime").date(2026, 3, 1))
    assert "PAST_DUE" in {f.code for f in validate(inv, conn, later)}


def test_near_threshold_band(conn, make):
    inv = make(total=9900.0, subtotal=9900.0,
               line_items=[LineItem(item="SteelBracket", quantity=220, unit_price=45.0,
                                    amount=9900.0)])
    assert "NEAR_THRESHOLD" in {f.code for f in validate(inv, conn)}


def test_unknown_vendor_is_a_flag_not_a_hard_fail(conn, make):
    findings = validate(make(vendor="Brand New Vendor LLC"), conn)
    assert [f.severity for f in findings if f.code == "UNKNOWN_VENDOR"] == [Severity.WARNING]
    assert not hard_fails(findings)


# --- duplicate / revision handling --------------------------------------------------


def test_exact_duplicate_across_formats_is_a_hard_fail(conn, load):
    record(conn, "r1", load("invoice_1011.txt"), Outcome.PAID, [])
    dup = check_duplicate(load("invoice_1011.pdf"), conn)
    assert [f.code for f in dup] == ["DUPLICATE_INVOICE"]
    assert dup[0].severity == Severity.CRITICAL and not dup[0].disputable


def test_revision_is_flagged_with_the_delta(conn, load):
    record(conn, "r1", load("invoice_1004.json"), Outcome.PAID, [])
    findings = check_duplicate(load("invoice_1004_revised.json"), conn)
    assert [f.code for f in findings] == ["REVISION_OF_PRIOR"]
    assert "+$4,050.00" in findings[0].message  # 5,940 - 1,890


def test_same_number_different_vendor_is_flagged(conn, load, make):
    record(conn, "r1", load("invoice_1004.json"), Outcome.PAID, [])
    other = make(invoice_number="INV-1004", vendor="Some Other Vendor")
    assert [f.code for f in check_duplicate(other, conn)] == ["INVOICE_NUMBER_COLLISION"]


def test_no_prior_means_no_duplicate_findings(conn, load):
    assert check_duplicate(load("invoice_1001.txt"), conn) == []


# --- batch stock reservation -------------------------------------------------------


def test_reserve_stock_flags_contention_only_when_enabled(conn, load, make):
    record(conn, "r1", make(invoice_number="INV-9100"), Outcome.PAID, [])  # claims 4 WidgetA
    big = make(invoice_number="INV-9101", line_items=[
        LineItem(item="WidgetA", quantity=14, unit_price=250.0, amount=3500.0)],
        subtotal=3500.0, total=3500.0)
    assert not hard_fails(validate(big, conn))  # off by default: 14 <= 15
    reserving = dataclasses.replace(SETTINGS, reserve_stock=True)
    assert "STOCK_CONTENTION" in {f.code for f in hard_fails(validate(big, conn, reserving))}


def test_disputable_markers(conn, load):
    by_code = {f.code: f for f in validate(load("invoice_1016.json"), conn)}
    assert by_code["UNKNOWN_ITEM"].disputable
    by_code = {f.code: f for f in validate(load("invoice_1009.json"), conn)}
    assert not by_code["NON_POSITIVE_TOTAL"].disputable
