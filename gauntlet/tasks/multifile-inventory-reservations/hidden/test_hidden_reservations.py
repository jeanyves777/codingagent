import pytest

from shop.catalog import Catalog, Product
from shop.errors import OutOfStock, ShopError
from shop.inventory import Inventory
from shop.orders import OrderService
from shop.reports import stock_report


def make_shop():
    catalog, inventory = Catalog(), Inventory()
    for sku, name, price, stock in [("C3", "Cheese", 700, 1), ("A1", "Apple", 50, 10), ("B2", "Bread", 250, 2)]:
        catalog.add(Product(sku, name, price))
        inventory.add_stock(sku, stock)
    return inventory, OrderService(catalog, inventory)


def test_reserve_and_release_change_availability_only():
    inventory, _ = make_shop()
    inventory.reserve("A1", 3)
    assert (inventory.available("A1"), inventory.stock("A1")) == (7, 10)
    inventory.release("A1", 2)
    assert inventory.available("A1") == 9


def test_order_is_all_or_nothing():
    inventory, orders = make_shop()
    with pytest.raises(OutOfStock) as raised:
        orders.place_order([("A1", 5), ("B2", 3)])
    assert raised.value.sku == "B2" and isinstance(raised.value, ShopError)
    assert inventory.available("A1") == 10 and inventory.available("B2") == 2


def test_out_of_stock_counts_existing_reservations():
    inventory, orders = make_shop()
    orders.place_order([("C3", 1)])
    with pytest.raises(OutOfStock):
        orders.place_order([("C3", 1)])


def test_cancel_releases_once():
    inventory, orders = make_shop()
    order = orders.place_order([("A1", 4), ("B2", 2)])
    orders.cancel(order)
    assert inventory.available("A1") == 10 and inventory.available("B2") == 2
    with pytest.raises(ShopError):
        orders.cancel(order)
    with pytest.raises(ShopError):
        orders.cancel(999)


def test_stock_report():
    inventory, orders = make_shop()
    orders.place_order([("A1", 4)])
    assert stock_report(inventory) == ["A1: 6/10 (reserved 4)", "B2: 2/2 (reserved 0)", "C3: 1/1 (reserved 0)"]
