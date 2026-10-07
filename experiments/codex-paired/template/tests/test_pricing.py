from inventory.pricing import apply_discount, bulk_price


def test_apply_discount():
    assert apply_discount(100, 10) == 90.0
    assert apply_discount(19.99, 25) == 14.99


def test_bulk_price():
    assert bulk_price(2.0, 5) == 10.0
    assert bulk_price(2.0, 10) == 18.0
    assert bulk_price(1.0, 100) == 80.0
