"""Unfinished synthetic receipt calculation; no external dependencies."""


def total_cents(rows):
    total = 0
    for row in rows:
        price = row["unit_cents"]
        quantity = row["quantity"]
        if price < 0 or quantity < 0:
            raise ValueError("invalid receipt")
        total += price * (quantity or 1)
    return total
