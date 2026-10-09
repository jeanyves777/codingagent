"""Errors raised by the shop package."""


class ShopError(Exception):
    """Base class for shop errors."""


class OutOfStock(ShopError):
    def __init__(self, sku: str):
        super().__init__(f"not enough stock for {sku}")
        self.sku = sku
