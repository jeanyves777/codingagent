from pricing import total_price


def test_total_price_exists():
    assert total_price([(3, 2)]) == 6
