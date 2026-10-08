import inspect
import warnings

import pytest

import checkout
from pricing import calculate_total, total_price


def test_total_price():
    assert total_price([(2.5, 2), (1, 3)]) == 8


def test_alias_warns():
    with pytest.warns(DeprecationWarning):
        assert calculate_total([(1, 1)]) == 1


def test_checkout_uses_new_name():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert checkout.checkout([(2, 2)]) == {"total": 4, "items": 1}
    assert "total_price" in inspect.getsource(checkout)
