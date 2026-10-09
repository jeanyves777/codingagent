"""Stock levels per sku."""


class Inventory:
    def __init__(self):
        self._stock = {}
        self._reserved = {}

    def add_stock(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._stock[sku] = self._stock.get(sku, 0) + quantity

    def stock(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserved(self, sku: str) -> int:
        return self._reserved.get(sku, 0)

    def available(self, sku: str) -> int:
        return self.stock(sku) - self.reserved(sku)

    def reserve(self, sku: str, quantity: int) -> None:
        if quantity <= 0 or quantity > self.available(sku):
            raise ValueError(f"cannot reserve {quantity} of {sku}")
        self._reserved[sku] = self.reserved(sku) + quantity

    def release(self, sku: str, quantity: int) -> None:
        if quantity <= 0 or quantity > self.reserved(sku):
            raise ValueError(f"cannot release {quantity} of {sku}")
        self._reserved[sku] = self.reserved(sku) - quantity

    def skus(self) -> list[str]:
        return sorted(self._stock)
