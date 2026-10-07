from inventory.stock import low_stock


def test_low_stock_order():
    items = [{"name": "a", "qty": 4}, {"name": "b", "qty": 1}, {"name": "c", "qty": 9}, {"name": "d", "qty": 1}]
    assert low_stock(items) == ["b", "d", "a"]


def test_threshold_is_exclusive():
    assert low_stock([{"name": "x", "qty": 5}], threshold=5) == []
