"""Unit tests for exact monetary arithmetic in :mod:`money`."""

from __future__ import annotations

from decimal import Decimal

import pytest

from money import MAX_CENTS, MoneyError, from_cents, parse_cents, to_cents


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.01", 1),
        ("0.10", 10),
        ("0.99", 99),
        ("1", 100),
        (1, 100),
        (1.0, 100),
        ("19.99", 1999),
        ("100.00", 10000),
        ("1234.56", 123456),
        (Decimal("2.50"), 250),
        ("2.675", 268),
        ("0.005", 1),
        ("0.015", 2),
        ("0.014", 1),
        ("1000000000", MAX_CENTS),
    ],
)
def test_to_cents_converts(value, expected):
    assert to_cents(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        0,
        "0",
        "0.00",
        "0.004",
        -1,
        "-5.00",
        "-0.01",
        True,
        False,
        None,
        "",
        "   ",
        "abc",
        "1,000",
        "1e2.5",
        float("inf"),
        float("-inf"),
        float("nan"),
        "inf",
        "nan",
        "1000000000.01",
        Decimal("1E+20"),
        object(),
    ],
)
def test_to_cents_rejects(value):
    with pytest.raises(MoneyError):
        to_cents(value)


def test_to_cents_accepts_the_exact_ceiling():
    assert to_cents("1000000000") == MAX_CENTS


def test_to_cents_rejects_one_cent_over_the_ceiling():
    with pytest.raises(MoneyError):
        to_cents("1000000000.01")


@pytest.mark.parametrize(
    "cents,expected",
    [(0, 0.0), (1, 0.01), (10, 0.10), (99, 0.99), (435, 4.35), (1999, 19.99), (-250, -2.5)],
)
def test_from_cents_renders_exact_major_units(cents, expected):
    result = from_cents(cents)
    assert result == expected
    assert isinstance(result, float)


@pytest.mark.parametrize("cents", [1, 10, 99, 100, 435, 1999, 123456, MAX_CENTS])
def test_from_cents_then_to_cents_round_trips(cents):
    assert to_cents(from_cents(cents)) == cents


def test_parse_cents_returns_cents():
    assert parse_cents("12.34") == 1234


def test_parse_cents_names_the_field_on_failure():
    with pytest.raises(MoneyError, match="limit"):
        parse_cents("nope", field="limit")


def test_money_error_is_a_value_error():
    assert issubclass(MoneyError, ValueError)
