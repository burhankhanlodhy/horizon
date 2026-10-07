from inventory.report import build_report


def test_report():
    items = [
        {"name": "bolt", "qty": 3, "price": 0.5},
        {"name": "nut", "qty": 0, "price": 0.1},
        {"name": "free", "qty": 2, "price": 0},
        {"name": "gear", "qty": 1, "price": 12.25},
    ]
    assert build_report(items) == "bolt: 3 x 0.50 = 1.50\ngear: 1 x 12.25 = 12.25\nTOTAL: 13.75"
