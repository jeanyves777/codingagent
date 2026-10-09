"""Plain-text reports."""
from .inventory import Inventory


def stock_report(inventory: Inventory) -> list[str]:
    return [f"{sku}: {inventory.stock(sku)}" for sku in inventory.skus()]
