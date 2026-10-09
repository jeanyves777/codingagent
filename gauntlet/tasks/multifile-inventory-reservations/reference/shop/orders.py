"""Placing and cancelling orders."""
import itertools

from .catalog import Catalog
from .errors import OutOfStock, ShopError
from .inventory import Inventory


class OrderService:
    def __init__(self, catalog: Catalog, inventory: Inventory):
        self.catalog, self.inventory = catalog, inventory
        self.orders = {}
        self._ids = itertools.count(1)

    def place_order(self, lines: list[tuple[str, int]]) -> int:
        """Place an order for (sku, quantity) lines and return its id."""
        needed = {}
        for sku, quantity in lines:
            self.catalog.get(sku)  # unknown products raise KeyError
            needed[sku] = needed.get(sku, 0) + quantity
        for sku, quantity in needed.items():
            if quantity > self.inventory.available(sku):
                raise OutOfStock(sku)
        for sku, quantity in needed.items():
            self.inventory.reserve(sku, quantity)
        order_id = next(self._ids)
        self.orders[order_id] = {"lines": list(lines), "status": "placed"}
        return order_id

    def cancel(self, order_id: int) -> None:
        order = self.orders.get(order_id)
        if order is None or order["status"] != "placed":
            raise ShopError(f"order {order_id} cannot be cancelled")
        for sku, quantity in order["lines"]:
            self.inventory.release(sku, quantity)
        order["status"] = "cancelled"

    def total_cents(self, order_id: int) -> int:
        return sum(self.catalog.get(sku).price_cents * quantity for sku, quantity in self.orders[order_id]["lines"])
