"""Checkout flow."""
from pricing import total_price


def checkout(items):
    return {"total": total_price(items), "items": len(items)}
