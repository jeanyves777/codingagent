"""Stock levels per sku."""


class Inventory:
    def __init__(self):
        self._stock = {}

    def add_stock(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._stock[sku] = self._stock.get(sku, 0) + quantity

    def stock(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def available(self, sku: str) -> int:
        return self.stock(sku)

    def skus(self) -> list[str]:
        return sorted(self._stock)
