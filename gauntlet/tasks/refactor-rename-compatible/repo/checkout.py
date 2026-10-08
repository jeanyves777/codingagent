"""Checkout flow."""
from pricing import calculate_total


def checkout(items):
    return {"total": calculate_total(items), "items": len(items)}
