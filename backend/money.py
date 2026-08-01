"""Money: decimal strings, 2dp, end to end. No float ever touches a price.

Every price in this system crosses this module's boundary. The rules are strict on
purpose -- a value that is not already a well-formed 2dp string is a bug upstream,
not something to coerce quietly here. The compiler's validator is the one place
allowed to pad short decimals, and it does so before values reach this module.
"""

from decimal import Decimal, InvalidOperation
import re

CENTS = Decimal("0.01")
MONEY_RE = re.compile(r"^\d+\.\d{2}$")


class MoneyError(ValueError):
    """A value that should have been money was not."""


def is_money(value):
    """True only for a canonical 2dp non-negative decimal string."""
    return isinstance(value, str) and bool(MONEY_RE.match(value))


def parse(value):
    """Strict money string -> Decimal. Rejects floats, ints, and sloppy strings."""
    if isinstance(value, Decimal):
        return _assert_2dp(value)
    if not is_money(value):
        raise MoneyError(f"not a 2dp decimal string: {value!r}")
    return Decimal(value)


def to_string(value):
    """Decimal -> 2dp string. Raises rather than round a price silently."""
    if not isinstance(value, Decimal):
        raise MoneyError(f"expected Decimal, got {type(value).__name__}")
    return str(_assert_2dp(value))


def add(*values):
    total = Decimal("0.00")
    for value in values:
        total += parse(value)
    return _assert_2dp(total)


def multiply(price, quantity):
    """price x integer quantity. A non-integer quantity is never money-safe."""
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise MoneyError(f"quantity must be an int, got {quantity!r}")
    if quantity < 0:
        raise MoneyError(f"quantity must not be negative, got {quantity}")
    return _assert_2dp(parse(price) * quantity)


def total_of(line_items):
    """Sum of unit_price x quantity across line items, as a Decimal."""
    total = Decimal("0.00")
    for item in line_items:
        total += multiply(item["unit_price"], item["quantity"])
    return _assert_2dp(total)


def _assert_2dp(value):
    """Widen to exactly 2dp, but never narrow. Sub-cent precision is an error.

    Python's decimal has no ROUND_UNNECESSARY, so the exponent is checked directly:
    anything finer than 0.01 would have to be rounded, and rounding a price is
    exactly what this module exists to prevent.
    """
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):  # NaN / Infinity carry a string exponent
        raise MoneyError(f"not a finite decimal: {value}")
    if exponent < -2:
        raise MoneyError(f"value has sub-cent precision and will not be rounded: {value}")
    try:
        return value.quantize(CENTS)
    except InvalidOperation:
        raise MoneyError(f"value cannot be represented at 2dp: {value}")
