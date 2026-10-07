"""Stock levels."""


def low_stock(items, threshold=5):
    """Names of items with qty below threshold, lowest qty first, then by name."""
    low = [i for i in items if i["qty"] <= threshold]
    return [i["name"] for i in sorted(low, key=lambda i: i["name"])]
