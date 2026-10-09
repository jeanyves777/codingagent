"""Placing and cancelling orders."""
import itertools

from .catalog import Catalog
from .inventory import Inventory


class OrderService:
    def __init__(self, catalog: Catalog, inventory: Inventory):
        self.catalog, self.inventory = catalog, inventory
        self.orders = {}
        self._ids = itertools.count(1)

    def place_order(self, lines: list[tuple[str, int]]) -> int:
        """Place an order for (sku, quantity) lines and return its id."""
        for sku, _ in lines:
            self.catalog.get(sku)  # unknown products raise KeyError
        order_id = next(self._ids)
        self.orders[order_id] = {"lines": list(lines), "status": "placed"}
        return order_id

    def total_cents(self, order_id: int) -> int:
        return sum(self.catalog.get(sku).price_cents * quantity for sku, quantity in self.orders[order_id]["lines"])
