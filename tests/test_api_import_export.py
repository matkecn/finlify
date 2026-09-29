"""End-to-end tests for JSON export and idempotent import."""

from __future__ import annotations


def add(client, amount, kind="expense", category="Groceries", date="2026-09-30"):
    return client.post(
        "/api/transactions",
        json={"amount": amount, "type": kind, "category": category, "date": date},
    )


def test_export_shape_and_version(client):
    add(client, "5.00")
    body = client.get("/api/export").json()
    assert body["app"] == "finlify"
    assert body["export_version"] == 1
    assert set(body) == {
        "app",
        "export_version",
        "exported_at",
        "transactions",
        "categories",
        "budgets",
        "accounts",
    }


def test_export_carries_exact_cents(client):
    add(client, "19.99")
    txs = client.get("/api/export").json()["transactions"]
    assert txs[0]["amount_cents"] == 1999


def test_export_is_a_download(client):
    response = client.get("/api/export")
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "finlify-backup-" in disposition and disposition.endswith('.json"')


def test_export_of_empty_database_has_seeded_categories(client):
    body = client.get("/api/export").json()
    assert body["transactions"] == []
    assert body["budgets"] == []
    assert len(body["categories"]) == 19


def test_reimport_adds_nothing(client):
    add(client, "5.00")
    exported = client.get("/api/export").json()
    response = client.post("/api/import", json={**exported, "replace": False})
    assert response.status_code == 200
    assert response.json()["transactions"] == 0
    assert response.json()["categories"] == 0


def test_reimport_does_not_change_money(client):
    add(client, "5.00", kind="income", category="Salary")
    add(client, "2.00")
    before = client.get("/api/summary").json()
    exported = client.get("/api/export").json()
    client.post("/api/import", json={**exported, "replace": False})
    after = client.get("/api/summary").json()
    assert after["income_cents"] == before["income_cents"]
    assert after["expenses_cents"] == before["expenses_cents"]
    assert after["transaction_count"] == before["transaction_count"]


def test_import_replace_restores_previous_state(client):
    add(client, "5.00")
    exported = client.get("/api/export").json()
    add(client, "999.00")  # extra data added after the backup
    client.post("/api/import", json={**exported, "replace": True})
    summary = client.get("/api/summary").json()
    assert summary["expenses_cents"] == 500
    assert summary["transaction_count"] == 1


def test_import_creates_categories_before_validating_transactions(client):
    payload = {
        "replace": False,
        "categories": [{"name": "Gold", "kind": "expense"}],
        "transactions": [
            {"amount": "5.00", "type": "expense", "category": "Gold", "date": "2026-09-30"}
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["categories"] == 1
    assert stats["transactions"] == 1
    assert stats["skipped"] == 0


def test_import_skips_unknown_category(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [
            {"amount": "5.00", "type": "expense", "category": "Ghost", "date": "2026-09-30"}
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["transactions"] == 0 and stats["skipped"] >= 1


def test_import_skips_malformed_date(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [
            {"amount": "5.00", "type": "expense", "category": "Groceries", "date": "nonsense"}
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["transactions"] == 0 and stats["skipped"] >= 1


def test_import_skips_missing_amount(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [
            {"type": "expense", "category": "Groceries", "date": "2026-09-30"}
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["transactions"] == 0 and stats["skipped"] >= 1


def test_import_skips_invalid_type(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [
            {"amount": "5.00", "type": "gift", "category": "Groceries", "date": "2026-09-30"}
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["transactions"] == 0 and stats["skipped"] >= 1


def test_amount_cents_only_row_is_not_rescaled(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [
            {"amount_cents": 1234, "type": "expense", "category": "Groceries", "date": "2026-09-30"}
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["transactions"] == 1
    assert client.get("/api/summary").json()["expenses_cents"] == 1234


def test_import_budgets_only_once(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [],
        "budgets": [{"category": "Groceries", "limit_cents": 5000}],
    }
    first = client.post("/api/import", json=payload).json()
    second = client.post("/api/import", json=payload).json()
    assert first["budgets"] == 1
    assert second["budgets"] == 0


def test_import_skips_budget_for_unknown_category(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [],
        "budgets": [{"category": "Ghost", "limit_cents": 5000}],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["budgets"] == 0 and stats["skipped"] >= 1


def test_import_counts_every_skipped_row(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [
            {"amount": "5.00", "type": "expense", "category": "Ghost", "date": "2026-09-30"},
            {"amount": "5.00", "type": "gift", "category": "Groceries", "date": "2026-09-30"},
            {"type": "expense", "category": "Groceries", "date": "2026-09-30"},
        ],
        "budgets": [],
    }
    assert client.post("/api/import", json=payload).json()["skipped"] == 3


def test_export_import_round_trip_is_lossless(client):
    add(client, "19.99", category="Groceries")
    add(client, "1000.00", kind="income", category="Salary")
    client.put("/api/budgets/Groceries", json={"limit": "50.00"})
    exported = client.get("/api/export").json()

    client.post("/api/import", json={**exported, "replace": True})

    summary = client.get("/api/summary").json()
    assert summary["income_cents"] == 100000
    assert summary["expenses_cents"] == 1999
    budgets = client.get("/api/budgets").json()["budgets"]
    assert budgets[0]["limit_cents"] == 5000
