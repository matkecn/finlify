"""Fuzz tests for JSON import: malformed input must never corrupt the ledger."""

from __future__ import annotations

import random
from decimal import Decimal

RNG = random.Random(424242)

NASTY_AMOUNTS = [None, "abc", -5, 0, "", 3.14, 100, "1,000", 1e20, True]


def build_valid_payload(n_categories=3, n_transactions=40, n_budgets=2, seed_rng=None):
    """Return a well-formed backup payload with exact expected totals."""
    rng = seed_rng or random.Random(1)
    categories = [{"name": f"Cat{i}", "kind": "expense"} for i in range(n_categories)]
    transactions = []
    for i in range(n_transactions):
        transactions.append(
            {
                "amount": str(Decimal(rng.randint(1, 100_000)) / 100),
                "type": "income" if i % 2 else "expense",
                "category": f"Cat{i % n_categories}",
                "date": f"2026-09-{(i % 28) + 1:02d}",
                "description": f"row {i}",
            }
        )
    budgets = [
        {"category": f"Cat{i}", "limit_cents": rng.randint(1, 100_000)}
        for i in range(n_budgets)
    ]
    expected_income = sum(
        Decimal(t["amount"]) for t in transactions if t["type"] == "income"
    )
    expected_expenses = sum(
        Decimal(t["amount"]) for t in transactions if t["type"] == "expense"
    )
    payload = {
        "replace": False,
        "categories": categories,
        "transactions": transactions,
        "budgets": budgets,
    }
    return payload, expected_income, expected_expenses


def test_valid_payload_imports_cleanly(client):
    payload, _, _ = build_valid_payload()
    stats = client.post("/api/import", json=payload).json()
    assert stats == {
        "categories": 3,
        "transactions": 40,
        "budgets": 2,
        "accounts": 0,
        "skipped": 0,
    }


def test_import_totals_match_the_payload(client):
    payload, income, expenses = build_valid_payload()
    client.post("/api/import", json=payload)
    summary = client.get("/api/summary").json()
    assert summary["income"] == float(income)
    assert summary["expenses"] == float(expenses)


def test_reimport_is_idempotent_for_every_entity(client):
    payload, _, _ = build_valid_payload()
    client.post("/api/import", json=payload)
    stats = client.post("/api/import", json=payload).json()
    assert stats["categories"] == 0
    assert stats["transactions"] == 0
    assert stats["budgets"] == 0
    assert stats["skipped"] == 43  # 3 existing categories + 40 existing transactions


def test_replace_import_restores_exact_totals(client):
    payload, income, expenses = build_valid_payload()
    client.post("/api/import", json=payload)
    client.post(
        "/api/transactions",
        json={"amount": "5000", "type": "expense", "category": "Cat1", "date": "2026-09-30"},
    )
    client.post("/api/import", json={**payload, "replace": True})
    summary = client.get("/api/summary").json()
    assert summary["income"] == float(income)
    assert summary["expenses"] == float(expenses)


def test_random_malformed_rows_are_skipped_not_fatal(client):
    rows = []
    for _ in range(200):
        rows.append(
            {
                "amount": RNG.choice(NASTY_AMOUNTS),
                "amount_cents": RNG.choice([None, "abc", -1, 1234, 0]),
                "type": RNG.choice(["income", "expense", "gift", None, ""]),
                "category": RNG.choice(["Groceries", "Ghost", None, "", "Dining"]),
                "date": RNG.choice(["2026-09-30", "nonsense", None, "2026-13-01", ""]),
                "description": RNG.choice([None, "x", "", "y" * 200]),
            }
        )
    response = client.post(
        "/api/import", json={"replace": False, "categories": [], "transactions": rows, "budgets": []}
    )
    assert response.status_code == 200
    stats = response.json()
    assert stats["transactions"] + stats["skipped"] == 200


def test_malformed_import_leaves_no_partial_garbage(client):
    rows = [
        {"amount": "5", "type": "expense", "category": "Ghost", "date": "2026-09-30"},
        {"amount": "bad", "type": "expense", "category": "Groceries", "date": "2026-09-30"},
        {"amount": "5", "type": "gift", "category": "Groceries", "date": "2026-09-30"},
    ]
    client.post("/api/import", json={"replace": False, "categories": [], "transactions": rows, "budgets": []})
    assert client.get("/api/summary").json()["transaction_count"] == 0


def test_export_then_replace_round_trips_losslessly(client):
    payload, _, _ = build_valid_payload()
    client.post("/api/import", json=payload)
    before = client.get("/api/summary").json()
    exported = client.get("/api/export").json()
    client.post("/api/import", json={**exported, "replace": True})
    after = client.get("/api/summary").json()
    assert after == before


def test_budget_import_accepts_major_and_minor_keys(client):
    client.post(
        "/api/import",
        json={
            "replace": False,
            "categories": [],
            "transactions": [],
            "budgets": [
                {"category": "Groceries", "limit": "25.00"},
                {"category": "Dining", "limit_cents": 1999},
            ],
        },
    )
    budgets = {b["category"]: b["limit_cents"] for b in client.get("/api/budgets").json()["budgets"]}
    assert budgets == {"Groceries": 2500, "Dining": 1999}


def test_repeated_fuzz_imports_never_inflate_a_balanced_ledger(client):
    payload, income, expenses = build_valid_payload()
    for _ in range(5):
        client.post("/api/import", json=payload)
    summary = client.get("/api/summary").json()
    assert summary["income"] == float(income)
    assert summary["expenses"] == float(expenses)
    assert summary["transaction_count"] == 40
