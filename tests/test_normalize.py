import datetime as dt

import pytest

from invoice_ap.normalize import (
    canonical_invoice_number,
    date_is_ambiguous,
    item_key,
    parse_date,
    parse_money,
    repair_ocr,
    split_item_note,
    split_vendor,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$3,500.O0", 3500.0),  # letter O for zero (INV-1012)
        ("$1,000.00", 1000.0),
        ("$250", 250.0),
        ("(250)", -250.0),
        (1890, 1890.0),
        (None, None),
        ("n/a", None),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-01-15", dt.date(2026, 1, 15)),
        ("Jan 30 2026", dt.date(2026, 1, 30)),
        ("January 27, 2026", dt.date(2026, 1, 27)),
        ("26-Jan-2O26", dt.date(2026, 1, 26)),  # letter O in the year (INV-1012)
        ("01/28/2026", dt.date(2026, 1, 28)),
        ("yesterday", None),  # relative: never guessed
        ("soon", None),
        (None, None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_ambiguous_slash_dates_are_detected():
    assert date_is_ambiguous("03/04/2026")
    assert not date_is_ambiguous("01/28/2026")
    assert not date_is_ambiguous("2026-03-04")


@pytest.mark.parametrize("raw", ["INV-1012", "INV 1012", "inv-1012", "1012"])
def test_invoice_number_is_canonical(raw):
    assert canonical_invoice_number(raw) == "INV-1012"


def test_item_names_match_on_case_whitespace_punctuation_only():
    assert item_key("Widget A") == item_key("widgeta") == item_key("WidgetA")
    assert item_key("WidgetA (rush order)") == "widgeta"
    # No fuzzy matching: a one-edit neighbour must stay distinct.
    assert item_key("WidgetC") != item_key("WidgetA")


def test_split_helpers():
    assert split_item_note("WidgetA (rush order)") == ("WidgetA", "rush order")
    assert split_vendor("QuickShip Distributers (formerly FastShip Ltd.)") == (
        "QuickShip Distributers",
        ["FastShip Ltd."],
    )


def test_ocr_repair_leaves_words_alone():
    assert repair_ocr("Oct Online") == "Oct Online"
    assert repair_ocr("2O26") == "2026"
