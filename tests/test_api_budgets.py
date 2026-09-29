"""End-to-end tests for the budget endpoints."""

from __future__ import annotations

from datetime import date

import pytest


def this_month() -> str:
    return date.today().replace(day=1).isoformat()


def spend(client, amount, category="Groceries"):
    return client.post(
        "/api/transactions",
        json={"amount": amount, "type": "expense", "category": category, "date": this_month()},
    )


def test_set_budget(client):
    response = client.put("/api/budgets/Groceries", json={"limit": "20.00"})
    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "Groceries"
    assert body["limit_cents"] == 2000
    assert body["limit"] == 20.0


def test_update_existing_budget(client):
    client.put("/api/budgets/Groceries", json={"limit": "20.00"})
    body = client.put("/api/budgets/Groceries", json={"limit": "35.50"}).json()
    assert body["limit_cents"] == 3550


def test_zero_limit_clears_tracking(client):
    client.put("/api/budgets/Groceries", json={"limit": "20.00"})
    body = client.put("/api/budgets/Groceries", json={"limit": "0"}).json()
    assert body["limit_cents"] == 0


def test_negative_limit_is_rejected(client):
    assert client.put("/api/budgets/Groceries", json={"limit": "-1"}).status_code == 422


def test_non_numeric_limit_is_rejected(client):
    assert client.put("/api/budgets/Groceries", json={"limit": "lots"}).status_code == 422


def test_unknown_category_is_rejected(client):
    assert client.put("/api/budgets/Ghost", json={"limit": "5"}).status_code == 422


def test_budget_is_stored_as_exact_cents(client):
    assert client.put("/api/budgets/Groceries", json={"limit": "19.99"}).json()["limit_cents"] == 1999


def test_list_returns_budget_with_spend(client):
    client.put("/api/budgets/Groceries", json={"limit": "100.00"})
    spend(client, "25.00")
    row = client.get("/api/budgets").json()["budgets"][0]
    assert row["spent"] == 25.0
    assert row["remaining"] == 75.0


@pytest.mark.parametrize(
    "amount,state",
    [("25.00", "ok"), ("50.00", "ok"), ("79.99", "ok"), ("85.00", "warn"), ("100.00", "warn"), ("150.00", "over")],
)
def test_budget_state_thresholds(client, amount, state):
    client.put("/api/budgets/Groceries", json={"limit": "100.00"})
    spend(client, amount)
    assert client.get("/api/budgets").json()["budgets"][0]["state"] == state


def test_budget_state_is_ok_with_no_spend(client):
    client.put("/api/budgets/Groceries", json={"limit": "100.00"})
    row = client.get("/api/budgets").json()["budgets"][0]
    assert row["state"] == "ok" and row["spent"] == 0.0 and row["pct"] == 0.0


def test_summary_carries_budget_status(client):
    client.put("/api/budgets/Groceries", json={"limit": "50.00"})
    spend(client, "10.00")
    budgets = client.get("/api/summary").json()["budgets"]
    assert budgets[0]["category"] == "Groceries"
    assert budgets[0]["limit_cents"] == 5000


def test_only_current_month_spend_counts(client):
    client.put("/api/budgets/Groceries", json={"limit": "100.00"})
    client.post(
        "/api/transactions",
        json={"amount": "60", "type": "expense", "category": "Groceries", "date": "2000-01-01"},
    )
    row = client.get("/api/budgets").json()["budgets"][0]
    assert row["spent"] == 0.0


def test_delete_budget(client):
    client.put("/api/budgets/Groceries", json={"limit": "20"})
    assert client.delete("/api/budgets/Groceries").json() == {"deleted": "Groceries"}
    assert client.get("/api/budgets").json()["budgets"] == []


def test_delete_missing_budget_returns_404(client):
    assert client.delete("/api/budgets/Groceries").status_code == 404
