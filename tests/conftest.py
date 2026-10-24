import pytest

from invoice_ap.config import INVOICE_DIR
from invoice_ap.db import init_db
from invoice_ap.models import Invoice, LineItem
from invoice_ap.readers import read_invoice


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def load():
    return lambda name: read_invoice(INVOICE_DIR / name)


def make_invoice(**overrides) -> Invoice:
    """A clean, in-stock, in-policy invoice; override fields to break one thing."""
    base = dict(
        invoice_number="INV-9001",
        vendor="Widgets Inc.",
        date="2026-01-15",
        due_date="2026-02-14",
        line_items=[LineItem(item="WidgetA", quantity=4, unit_price=250.0, amount=1000.0)],
        subtotal=1000.0,
        tax=0.0,
        total=1000.0,
        payment_terms="Net 30",
        source_file="synthetic.json",
    )
    base.update(overrides)
    return Invoice(**base)


@pytest.fixture
def make():
    return make_invoice
