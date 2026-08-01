"""Money handling — SPEC working agreement: decimal strings, 2dp, no floats.

These are the tests that stop a float ever touching a price.
"""

import pytest

from backend.money import MoneyError, add, is_money, multiply, parse, to_string


# --- parsing and validation -------------------------------------------------


@pytest.mark.parametrize("value", ["0.00", "12.50", "9999999.99", "0.01"])
def test_valid_2dp_strings_round_trip_unchanged(value):
    assert to_string(parse(value)) == value
    assert is_money(value)


@pytest.mark.parametrize(
    "value",
    [
        "12.5",  # 1dp
        "12",  # 0dp
        "12.500",  # 3dp
        "-1.00",  # negative
        "1,200.00",  # thousands separator
        " 12.50",  # whitespace
        "12.50 ",
        "$12.50",  # currency symbol
        "1e2",  # scientific
        "abc",
        "",
        "NaN",
        "Infinity",
    ],
)
def test_malformed_money_is_rejected(value):
    assert not is_money(value)
    with pytest.raises(MoneyError):
        parse(value)


@pytest.mark.parametrize("value", [12.5, 12, None, True, [], {}])
def test_non_strings_are_rejected_including_floats(value):
    """A float reaching this boundary is the bug the whole rule exists to prevent."""
    assert not is_money(value)
    with pytest.raises(MoneyError):
        parse(value)


# --- arithmetic -------------------------------------------------------------


def test_multiply_by_quantity_is_exact():
    assert to_string(multiply("7.25", 2)) == "14.50"
    assert to_string(multiply("0.01", 3)) == "0.03"


def test_add_is_exact_where_float_addition_would_drift():
    """0.1 + 0.2 == 0.30, not 0.30000000000000004."""
    assert to_string(add(parse("0.10"), parse("0.20"))) == "0.30"


def test_sum_of_line_items_is_exact_over_many_items():
    total = parse("0.00")
    for _ in range(10):
        total = add(total, multiply("0.07", 1))
    assert to_string(total) == "0.70"


def test_multiply_rejects_non_integer_quantity():
    with pytest.raises(MoneyError):
        multiply("7.25", 2.0)
    with pytest.raises(MoneyError):
        multiply("7.25", "2")


def test_multiply_rejects_negative_quantity():
    with pytest.raises(MoneyError):
        multiply("7.25", -1)


def test_to_string_always_emits_two_decimals():
    assert to_string(parse("5.00")) == "5.00"
    assert to_string(multiply("5.00", 2)) == "10.00"
