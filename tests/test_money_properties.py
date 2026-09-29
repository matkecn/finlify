"""Property and fuzz tests for the exact monetary arithmetic.

Values are generated from a fixed seed, so a failure is reproducible.
"""

from __future__ import annotations

import random
from decimal import ROUND_HALF_UP, Decimal

import pytest

from money import MAX_CENTS, MoneyError, from_cents, to_cents

RNG = random.Random(20260930)


def reference_cents(value: Decimal) -> int:
    """Round a decimal to cents the way commercial accounting expects."""
    return int(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def random_cents(count: int) -> list[int]:
    return [RNG.randint(1, MAX_CENTS) for _ in range(count)]


@pytest.mark.parametrize("cents", random_cents(200))
def test_from_cents_then_to_cents_is_identity(cents):
    assert to_cents(from_cents(cents)) == cents


@pytest.mark.parametrize("cents", random_cents(200))
def test_from_cents_is_the_exact_two_decimal_value(cents):
    rendered = Decimal(str(from_cents(cents)))
    assert rendered == (Decimal(cents) / 100).quantize(Decimal("0.01"))


@pytest.mark.parametrize("cents", random_cents(100))
def test_to_cents_string_equals_reference(cents):
    amount = (Decimal(cents) / 100).quantize(Decimal("0.01"))
    assert to_cents(str(amount)) == reference_cents(amount)


def test_rounding_is_always_half_up():
    for _ in range(500):
        third = RNG.randint(0, 9)
        amount = Decimal(f"{RNG.randint(1, 9999)}.{RNG.randint(0, 9)}{RNG.randint(0, 9)}{third}")
        assert to_cents(str(amount)) == reference_cents(amount)


def test_half_cent_always_rounds_up():
    for whole in range(0, 1000, 7):
        assert to_cents(f"{whole}.005") == whole * 100 + 1


def test_just_below_half_cent_rounds_down():
    for whole in range(7, 1000, 7):
        assert to_cents(f"{whole}.004999") == whole * 100


def test_conversion_is_monotonic():
    values = sorted((Decimal(RNG.randint(1, MAX_CENTS)) / 100 for _ in range(300)))
    cents = [to_cents(str(v)) for v in values]
    assert cents == sorted(cents)


def test_cents_are_always_positive_and_integral():
    for cents in random_cents(200):
        result = to_cents(str(Decimal(cents) / 100))
        assert isinstance(result, int)
        assert result > 0


def test_summing_parts_matches_summing_cents():
    parts = [(Decimal(RNG.randint(1, 500000)) / 100).quantize(Decimal("0.01")) for _ in range(500)]
    assert sum(to_cents(str(p)) for p in parts) == reference_cents(sum(parts))


@pytest.mark.parametrize("cents", [MAX_CENTS, MAX_CENTS - 1, MAX_CENTS // 2, 1])
def test_boundary_values_round_trip(cents):
    assert to_cents(from_cents(cents)) == cents


def test_everything_above_the_cap_is_rejected():
    for _ in range(50):
        over = Decimal(MAX_CENTS + RNG.randint(1, 1_000_000)) / 100
        with pytest.raises(MoneyError):
            to_cents(str(over))


def test_everything_at_or_below_the_cap_is_accepted():
    for _ in range(50):
        under = Decimal(RNG.randint(1, MAX_CENTS)) / 100
        assert to_cents(str(under)) >= 1
