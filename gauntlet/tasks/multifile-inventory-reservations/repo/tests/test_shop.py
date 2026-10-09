from shop.catalog import Catalog, Product
from shop.inventory import Inventory
from shop.orders import OrderService


def make_shop():
    catalog, inventory = Catalog(), Inventory()
    catalog.add(Product("A1", "Apple", 50))
    catalog.add(Product("B2", "Bread", 250))
    inventory.add_stock("A1", 10)
    inventory.add_stock("B2", 2)
    return catalog, inventory, OrderService(catalog, inventory)


def test_order_total():
    _, _, orders = make_shop()
    order = orders.place_order([("A1", 3), ("B2", 1)])
    assert orders.total_cents(order) == 400


def test_placing_an_order_reserves_stock():
    _, inventory, orders = make_shop()
    orders.place_order([("A1", 4)])
    assert inventory.available("A1") == 6
    assert inventory.stock("A1") == 10
