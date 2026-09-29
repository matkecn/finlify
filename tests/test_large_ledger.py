"""Large-ledger tests: exactness and pagination at volume."""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

RNG = random.Random(20260930)

EXPENSE_CATEGORIES = ["Groceries", "Dining", "Transport", "Shopping"]


def today_iso() -> str:
    return date.today().isoformat()


def post(client, cents, kind, category):
    return client.post(
        "/api/transactions",
        json={
            "amount": str(Decimal(cents) / 100),
            "type": kind,
            "category": category,
            "date": today_iso(),
        },
    )


def seed_ledger(client, count=300):
    """Insert ``count`` transactions and return the exact expected totals."""
    income = expenses = 0
    for index in range(count):
        cents = RNG.randint(1, 100_000)
        if index % 3 == 0:
            post(client, cents, "income", "Salary")
            income += cents
        else:
            post(client, cents, "expense", EXPENSE_CATEGORIES[index % len(EXPENSE_CATEGORIES)])
            expenses += cents
    return income, expenses


def test_large_ledger_totals_are_exact(client):
    income, expenses = seed_ledger(client)
    summary = client.get("/api/summary").json()
    assert summary["income_cents"] == income
    assert summary["expenses_cents"] == expenses
    assert summary["balance_cents"] == income - expenses
    assert summary["transaction_count"] == 300
    assert summary["classified_count"] == 300


def test_large_ledger_paginates(client):
    seed_ledger(client)
    page = client.get("/api/transactions?limit=25&offset=0").json()
    assert page["total"] == 300
    assert len(page["items"]) == 25
    assert page["has_more"] is True

    last = client.get("/api/transactions?limit=25&offset=275").json()
    assert len(last["items"]) == 25
    assert last["has_more"] is False


def test_large_ledger_full_page_of_two_hundred(client):
    seed_ledger(client)
    page = client.get("/api/transactions?limit=200").json()
    assert len(page["items"]) == 200
    assert page["has_more"] is True


def test_large_ledger_breakdown_accounts_for_every_expense(client):
    seed_ledger(client)
    breakdown = client.get("/api/breakdown?months=1&type=expense").json()["categories"]
    assert sum(row["count"] for row in breakdown) == 200  # 2 of every 3 rows


def test_large_ledger_timeseries_count_matches(client):
    seed_ledger(client)
    series = client.get("/api/timeseries?months=1").json()["series"]
    assert series[-1]["count"] == 300


def test_large_ledger_totals_survive_a_backup_round_trip(client):
    income, expenses = seed_ledger(client)
    exported = client.get("/api/export").json()
    client.post("/api/import", json={**exported, "replace": True})
    summary = client.get("/api/summary").json()
    assert summary["income_cents"] == income
    assert summary["expenses_cents"] == expenses
    assert summary["transaction_count"] == 300
