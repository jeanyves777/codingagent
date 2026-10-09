import pytest

from invoice import Invoice
from money import Money


def test_money():
    assert str(Money(1234, "USD")) == "USD 12.34"
    assert str(Money(5, "EUR")) == "EUR 0.05"
    assert Money(100, "USD").add(Money(250, "USD")) == Money(350, "USD")
    with pytest.raises(ValueError):
        Money(1, "USD").add(Money(1, "EUR"))


def test_invoice_total():
    invoice = Invoice("EUR")
    invoice.add_line("a", 150)
    invoice.add_line("b", 275)
    assert invoice.total() == Money(425, "EUR")
