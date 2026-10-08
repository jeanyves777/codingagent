from money import Money


def test_money_formats_and_adds():
    assert str(Money(250, "USD")) == "USD 2.50"
    assert Money(1, "USD").add(Money(2, "USD")) == Money(3, "USD")
