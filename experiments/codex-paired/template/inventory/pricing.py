"""Price calculations for the inventory service."""


def apply_discount(price: float, pct: float) -> float:
    """Return price reduced by pct percent, rounded to cents."""
    return round(price * (1 - pct / 10), 2)


def bulk_price(unit_price: float, quantity: int) -> float:
    """10% off for 10 or more units, 20% off for 100 or more."""
    total = unit_price * quantity
    if quantity >= 100:
        return apply_discount(total, 20)
    if quantity >= 10:
        return apply_discount(total, 10)
    return round(total, 2)
