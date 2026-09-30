"""Exact monetary arithmetic for Finlify.

Every amount is persisted and aggregated as an integer number of minor units
(cents). Binary floating point cannot represent 0.10 or 19.99 exactly, so
floats are accepted only at the API boundary and immediately converted.
Rounding is half-up, matching commercial accounting expectations where a
half cent rounds to the next whole cent.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENTS = Decimal("0.01")
MAX_CENTS = 100_000_000_000


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as a usable monetary amount."""


def _quantized_cents(value: Decimal) -> int:
    """Round a parsed amount to whole cents.

    Raises:
        MoneyError: If the amount is so large it cannot be expressed in cents.
            ``Decimal`` signals this with an ``InvalidOperation`` rather than
            a plain overflow, which would otherwise escape as an unexpected
            exception type for a value that is simply unusable.
    """
    try:
        return int(value.quantize(CENTS, rounding=ROUND_HALF_UP) * 100)
    except InvalidOperation:
        raise MoneyError("Amount is unrealistically large")


def to_cents(value) -> int:
    """Convert a user-supplied amount into integer cents using half-up rounding.

    Args:
        value: The amount to convert. ``str``, ``int``, ``float`` and
            ``Decimal`` are accepted; ``bool`` is rejected.

    Returns:
        The amount expressed as a positive integer number of cents.

    Raises:
        MoneyError: If the value is not numeric, not finite, not strictly
            positive, or greater than :data:`MAX_CENTS`.

    Floats are routed through ``str()`` so that ``19.99`` becomes
    ``Decimal("19.99")`` rather than its binary approximation.
    """
    if isinstance(value, bool):
        raise MoneyError("Amount must be a number")
    try:
        if isinstance(value, float):
            dec = Decimal(str(value))
        else:
            dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise MoneyError("Amount must be a valid number")

    if not dec.is_finite():
        raise MoneyError("Amount must be a finite number")

    cents = _quantized_cents(dec)

    if cents <= 0:
        raise MoneyError("Amount must be greater than zero")
    if cents > MAX_CENTS:
        raise MoneyError("Amount is unrealistically large")
    return cents


def to_signed_cents(value) -> int:
    """Convert an amount that may be zero or negative into integer cents.

    Used for opening balances, where zero and negative values are meaningful
    (a credit account can open in the red). Rounding is half-up, matching
    :func:`to_cents`, and the magnitude is capped at :data:`MAX_CENTS`.

    Raises:
        MoneyError: If the value is not a finite, representable amount.
    """
    if isinstance(value, bool):
        raise MoneyError("Amount must be a number")
    try:
        if isinstance(value, float):
            dec = Decimal(str(value))
        else:
            dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise MoneyError("Amount must be a valid number")

    if not dec.is_finite():
        raise MoneyError("Amount must be a finite number")

    cents = _quantized_cents(dec)
    if abs(cents) > MAX_CENTS:
        raise MoneyError("Amount is unrealistically large")
    return cents


def from_cents(cents: int) -> float:
    """Convert integer cents to a float suitable for JSON display.

    The result is for presentation and client-side charting only. It must never
    be fed back into arithmetic; use :func:`to_cents` for that.
    """
    return float((Decimal(int(cents)) / 100).quantize(CENTS))


def parse_cents(raw, field: str = "amount") -> int:
    """Convert ``raw`` to cents, prefixing any error with the field name.

    Args:
        raw: The untrusted value to convert.
        field: The request field name, used to make errors actionable.

    Returns:
        The amount expressed as a positive integer number of cents.

    Raises:
        MoneyError: If conversion fails, with ``field`` named in the message.
    """
    try:
        return to_cents(raw)
    except MoneyError as exc:
        raise MoneyError(f"{field}: {exc}") from exc


def parse_signed_cents(raw, field: str = "amount") -> int:
    """Convert ``raw`` to signed cents, prefixing any error with the field name.

    The signed counterpart to :func:`parse_cents`, used for opening balances
    where zero and negative values are valid.

    Raises:
        MoneyError: If conversion fails, with ``field`` named in the message.
    """
    try:
        return to_signed_cents(raw)
    except MoneyError as exc:
        raise MoneyError(f"{field}: {exc}") from exc
