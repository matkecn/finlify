"""End-to-end tests for JSON export and idempotent import."""

from __future__ import annotations

from conftest import ADMIN_USERNAME
from models import CATEGORY_PALETTE


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
        "owner",
        "transactions",
        "categories",
        "budgets",
        "accounts",
        "goals",
    }
    assert body["owner"] == ADMIN_USERNAME


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


def test_import_keeps_category_with_unusable_colour(client):
    payload = {
        "replace": False,
        "categories": [
            {"name": "Gold", "kind": "expense", "color": '"><script>alert(1)</script>'}
        ],
        "transactions": [],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["categories"] == 1
    assert stats["skipped"] == 0
    gold = next(
        c for c in client.get("/api/categories").json()["categories"] if c["name"] == "Gold"
    )
    assert gold["color"] in CATEGORY_PALETTE


def test_import_keeps_account_with_unusable_colour(client):
    payload = {
        "replace": False,
        "categories": [],
        "accounts": [
            {"name": "Vault", "kind": "checking", "color": "teal;}</style><img src=x>"}
        ],
        "transactions": [],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["accounts"] == 1
    vault = next(
        a for a in client.get("/api/accounts").json()["accounts"] if a["name"] == "Vault"
    )
    assert vault["color"] in CATEGORY_PALETTE


def test_import_expands_shorthand_colour(client):
    payload = {
        "replace": False,
        "categories": [{"name": "Gold", "kind": "expense", "color": "#abc"}],
        "transactions": [],
        "budgets": [],
    }
    client.post("/api/import", json=payload)
    gold = next(
        c for c in client.get("/api/categories").json()["categories"] if c["name"] == "Gold"
    )
    assert gold["color"] == "#aabbcc"


def test_import_does_not_echo_hostile_colour_on_export(client):
    payload = {
        "replace": False,
        "categories": [
            {"name": "Gold", "kind": "expense", "color": "#000\"><script>alert(1)</script>"}
        ],
        "transactions": [],
        "budgets": [],
    }
    client.post("/api/import", json=payload)
    exported = client.get("/api/export").json()
    gold = next(c for c in exported["categories"] if c["name"] == "Gold")
    assert "<script>" not in gold["color"]
    assert gold["color"] in CATEGORY_PALETTE


def test_export_carries_goals(client):
    client.post("/api/goals", json={"name": "Holiday", "target": "1500.00", "deadline": "2027-06-30"})
    goals = client.get("/api/export").json()["goals"]
    assert len(goals) == 1
    assert goals[0]["name"] == "Holiday"
    assert goals[0]["target_cents"] == 150000
    assert goals[0]["deadline"] == "2027-06-30"


def test_import_adds_goals_once(client):
    payload = {"goals": [{"name": "Holiday", "target_cents": 150000}]}
    first = client.post("/api/import", json=payload).json()
    assert first["goals"] == 1
    assert client.post("/api/import", json=payload).json()["goals"] == 0
    assert client.post("/api/import", json=payload).json()["skipped"] == 1


def test_import_accepts_a_major_units_goal_target(client):
    stats = client.post("/api/import", json={"goals": [{"name": "Trip", "target": "99.99"}]}).json()
    assert stats["goals"] == 1
    assert client.get("/api/goals").json()["goals"][0]["target_cents"] == 9999


def test_import_skips_a_goal_for_an_unknown_account(client):
    payload = {"goals": [{"name": "Ghost", "target_cents": 100, "account": "Nowhere"}]}
    stats = client.post("/api/import", json=payload).json()
    assert stats["goals"] == 0
    assert stats["skipped"] == 1


def test_import_skips_a_goal_with_an_unusable_target(client):
    payload = {"goals": [
        {"name": "No target"},
        {"name": "Bad target", "target_cents": "not money"},
        {"name": "Negative", "target_cents": -5},
        {"name": "Zero", "target_cents": 0},
    ]}
    assert client.post("/api/import", json=payload).json()["skipped"] == 4


def test_import_skips_a_goal_with_an_unusable_deadline(client):
    payload = {"goals": [{"name": "Bad date", "target_cents": 100, "deadline": "next tuesday"}]}
    assert client.post("/api/import", json=payload).json()["goals"] == 0


def test_import_skips_a_blank_goal_name(client):
    payload = {"goals": [{"name": "  ", "target_cents": 100}]}
    assert client.post("/api/import", json=payload).json()["skipped"] == 1


def test_replace_import_restores_goals(client):
    client.post("/api/goals", json={"name": "Kept", "target": "50.00"})
    backup = client.get("/api/export").json()
    client.post("/api/goals", json={"name": "Added later", "target": "70.00"})
    stats = client.post("/api/import", json={**backup, "replace": True}).json()
    assert stats["goals"] == 1
    names = [g["name"] for g in client.get("/api/goals").json()["goals"]]
    assert names == ["Kept"]


def test_goal_survives_an_export_import_round_trip(client):
    client.post("/api/accounts", json={"name": "Savings", "opening_balance": "200.00"})
    client.post("/api/goals", json={
        "name": "Car",
        "target": "1000.00",
        "account": "Savings",
        "deadline": "2027-01-31",
    })
    backup = client.get("/api/export").json()
    client.post("/api/import", json={**backup, "replace": True})
    goal = client.get("/api/goals").json()["goals"][0]
    assert goal["name"] == "Car"
    assert goal["account"] == "Savings"
    assert goal["deadline"] == "2027-01-31"
    assert goal["saved_cents"] == 20000
