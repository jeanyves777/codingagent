"""Plain-text reports."""
from .inventory import Inventory


def stock_report(inventory: Inventory) -> list[str]:
    return [f"{sku}: {inventory.available(sku)}/{inventory.stock(sku)} (reserved {inventory.stock(sku) - inventory.available(sku)})"
            for sku in inventory.skus()]
