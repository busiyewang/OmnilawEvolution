from decimal import Decimal, ROUND_HALF_UP


def format_price(amount):
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "{:,.2f}".format(value)
