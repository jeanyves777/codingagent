"""Products the shop sells."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int


class Catalog:
    def __init__(self):
        self._products = {}

    def add(self, product: Product) -> None:
        self._products[product.sku] = product

    def get(self, sku: str) -> Product:
        return self._products[sku]

    def skus(self) -> list[str]:
        return sorted(self._products)
