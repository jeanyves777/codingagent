"""Pricing rules."""
import warnings


def total_price(items):
    """Sum price * quantity for each (price, quantity) pair."""
    return sum(price * quantity for price, quantity in items)


def calculate_total(items):
    """Deprecated alias for total_price."""
    warnings.warn("calculate_total is deprecated; use total_price", DeprecationWarning, stacklevel=2)
    return total_price(items)
