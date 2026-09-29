"""Unit tests for the ORM models' validation and derived properties."""

from __future__ import annotations

from datetime import date

import pytest

from models import Budget, Category, Transaction


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("coffee", "Coffee"),
        ("  coffee  ", "Coffee"),
        ("two   words", "Two words"),
        ("iPhone", "iPhone"),
        ("ATMs", "ATMs"),
        ("GROCERY", "GROCERY"),
        ("Rent 2026", "Rent 2026"),
    ],
)
def test_category_name_is_normalised(raw, expected):
    assert Category(name=raw).name == expected


def test_category_name_is_truncated_to_forty_characters():
    assert len(Category(name="a" * 80).name) == 40


def test_category_name_cannot_be_blank():
    with pytest.raises(ValueError):
        Category(name="   ")


@pytest.mark.parametrize("kind", ["income", "expense", "EXPENSE", " Income "])
def test_category_accepts_valid_kinds(kind):
    assert Category(name="X", kind=kind).kind in ("income", "expense")


@pytest.mark.parametrize("kind", ["crypto", "", "transfer"])
def test_category_rejects_invalid_kinds(kind):
    with pytest.raises(ValueError):
        Category(name="X", kind=kind)


def test_budget_normalises_category():
    assert Budget(category="  Coffee  ").category == "Coffee"


def test_budget_category_cannot_be_blank():
    with pytest.raises(ValueError):
        Budget(category="   ")


@pytest.mark.parametrize(
    "kind,classified",
    [("income", True), ("expense", True), ("string", False), ("", False)],
)
def test_transaction_is_classified(kind, classified):
    assert Transaction(amount_cents=100, type=kind).is_classified is classified


@pytest.mark.parametrize(
    "kind,amount,signed",
    [
        ("income", 1000, 1000),
        ("expense", 1000, -1000),
        ("expense", 1, -1),
        ("string", 1000, 0),
    ],
)
def test_transaction_signed_cents(kind, amount, signed):
    assert Transaction(amount_cents=amount, type=kind).signed_cents == signed


def test_transaction_to_dict_carries_exact_cents_and_classification():
    row = Transaction(
        id=7,
        amount_cents=435,
        type="expense",
        category="Coffee",
        description="Flat white",
        date=date(2026, 9, 30),
    )
    assert row.to_dict() == {
        "id": 7,
        "amount": 4.35,
        "amount_cents": 435,
        "type": "expense",
        "category": "Coffee",
        "description": "Flat white",
        "date": "2026-09-30",
        "classified": True,
    }


def test_category_to_dict_shape():
    row = Category(id=3, name="Coffee", kind="expense", color="#c084fc", sort_order=0)
    assert row.to_dict() == {
        "id": 3,
        "name": "Coffee",
        "kind": "expense",
        "color": "#c084fc",
        "is_archived": False,
        "sort_order": 0,
    }


def test_budget_to_dict_carries_exact_cents():
    row = Budget(id=1, category="Coffee", limit_cents=2000)
    assert row.to_dict() == {"id": 1, "category": "Coffee", "limit": 20.0, "limit_cents": 2000}
