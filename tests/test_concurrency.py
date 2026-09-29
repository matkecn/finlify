"""Concurrency tests for the money layer and the database write path."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

from database import SessionLocal
from models import Transaction
from money import to_cents


def test_to_cents_is_deterministic_under_threads():
    inputs = [str(Decimal(i) / 100) for i in range(1, 500)]
    expected = [to_cents(value) for value in inputs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(to_cents, inputs * 2))
    assert results == expected * 2


def _write_one(cents: int) -> None:
    session = SessionLocal()
    try:
        session.add(
            Transaction(
                amount_cents=cents,
                type="expense",
                category="Groceries",
                date=date.today(),
            )
        )
        session.commit()
    finally:
        session.close()


def test_concurrent_writes_are_all_persisted(client):
    count = 20
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_write_one, [100] * count))

    summary = client.get("/api/summary").json()
    assert summary["transaction_count"] == count
    assert summary["expenses_cents"] == 100 * count


def test_totals_stay_exact_after_concurrent_writes(client):
    amounts = [index * 37 + 1 for index in range(24)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_write_one, amounts))

    summary = client.get("/api/summary").json()
    assert summary["expenses_cents"] == sum(amounts)
