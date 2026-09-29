"""End-to-end tests for multi-account support."""

from __future__ import annotations


def create_account(client, name, **fields):
    return client.post("/api/accounts", json={"name": name, **fields})


def add_tx(client, amount, kind="expense", category="Groceries", account="Main", date="2026-09-30"):
    return client.post(
        "/api/transactions",
        json={
            "amount": amount,
            "type": kind,
            "category": category,
            "account": account,
            "date": date,
        },
    )


def test_default_main_account_is_seeded(client):
    body = client.get("/api/accounts").json()
    names = [a["name"] for a in body["accounts"]]
    assert names == ["Main"]


def test_new_database_exposes_main_account(client):
    body = client.get("/api/health").json()
    assert body["accounts"] == 1


def test_create_account_returns_balance_and_kind(client):
    created = create_account(client, "Rainy Day", kind="savings").json()
    assert created["name"] == "Rainy Day"
    assert created["kind"] == "savings"
    assert created["balance_cents"] == 0
    assert created["balance"] == 0.0
    assert created["opening_balance_cents"] == 0


def test_account_name_is_titlecased_and_whitespace_collapsed(client):
    created = create_account(client, "  rainy   day  ").json()
    assert created["name"] == "Rainy day"


def test_account_name_preserves_deliberate_capitalisation(client):
    created = create_account(client, "iCloud Wallet").json()
    assert created["name"] == "iCloud Wallet"


def test_duplicate_account_name_is_rejected(client):
    create_account(client, "Main")
    assert create_account(client, "Main").status_code == 409
    assert create_account(client, "main").status_code == 409


def test_unknown_account_kind_is_rejected(client):
    assert create_account(client, "Crypto", kind="magic").status_code == 422


def test_invalid_account_colour_is_rejected(client):
    assert create_account(client, "Crypto", color="teal").status_code == 422


def test_opening_balance_may_be_negative(client):
    created = create_account(client, "Credit Card", kind="credit", opening_balance="-250.00").json()
    assert created["opening_balance_cents"] == -25000
    assert created["balance_cents"] == -25000


def test_account_balance_is_opening_plus_signed_transactions(client):
    create_account(client, "Savings", opening_balance="100.00")
    add_tx(client, "50.00", kind="income", category="Salary", account="Savings")
    add_tx(client, "20.00", kind="expense", account="Savings")
    savings = next(a for a in client.get("/api/accounts").json()["accounts"] if a["name"] == "Savings")
    assert savings["income_cents"] == 5000
    assert savings["expenses_cents"] == 2000
    assert savings["net_cents"] == 3000
    assert savings["balance_cents"] == 13000
    assert savings["transaction_count"] == 2


def test_net_worth_sums_every_account(client):
    create_account(client, "Savings", opening_balance="1000.00")
    add_tx(client, "250.00", kind="income", category="Salary", account="Main")
    add_tx(client, "40.00", account="Main")
    body = client.get("/api/accounts").json()
    assert body["net_worth_cents"] == 100000 + 21000
    assert client.get("/api/summary").json()["net_worth_cents"] == body["net_worth_cents"]


def test_archived_accounts_are_hidden_by_default(client):
    created = create_account(client, "Old Card", kind="credit").json()
    client.put(f"/api/accounts/{created['id']}", json={"is_archived": True})
    assert [a["name"] for a in client.get("/api/accounts").json()["accounts"]] == ["Main"]
    names = [a["name"] for a in client.get("/api/accounts?include_archived=true").json()["accounts"]]
    assert names == ["Main", "Old Card"]


def test_archived_account_still_counts_towards_net_worth(client):
    created = create_account(client, "Closed", opening_balance="500.00").json()
    client.put(f"/api/accounts/{created['id']}", json={"is_archived": True})
    assert client.get("/api/accounts").json()["net_worth_cents"] == 50000


def test_renaming_an_account_repoints_its_transactions(client):
    created = create_account(client, "Wallet").json()
    add_tx(client, "5.00", account="Wallet")
    client.put(f"/api/accounts/{created['id']}", json={"name": "Cash"})
    items = client.get("/api/transactions").json()["items"]
    assert items[0]["account"] == "Cash"


def test_renaming_onto_an_existing_name_is_rejected(client):
    create_account(client, "Wallet")
    other = create_account(client, "Cash").json()
    assert client.put(f"/api/accounts/{other['id']}", json={"name": "Wallet"}).status_code == 409


def test_updating_opening_balance_and_kind(client):
    created = create_account(client, "Wallet").json()
    updated = client.put(
        f"/api/accounts/{created['id']}",
        json={"kind": "investment", "opening_balance": "75.50"},
    ).json()
    assert updated["kind"] == "investment"
    assert updated["opening_balance_cents"] == 7550


def test_delete_unused_account(client):
    created = create_account(client, "Temp").json()
    assert client.delete(f"/api/accounts/{created['id']}").status_code == 200
    assert "Temp" not in [a["name"] for a in client.get("/api/accounts").json()["accounts"]]


def test_delete_account_in_use_is_blocked(client):
    created = create_account(client, "Wallet").json()
    add_tx(client, "5.00", account="Wallet")
    assert client.delete(f"/api/accounts/{created['id']}").status_code == 409


def test_delete_missing_account_is_404(client):
    assert client.delete("/api/accounts/999").status_code == 404


def test_update_missing_account_is_404(client):
    assert client.put("/api/accounts/999", json={"name": "Nope"}).status_code == 404


def test_transaction_with_unknown_account_is_rejected(client):
    response = add_tx(client, "5.00", account="Ghost")
    assert response.status_code == 422
    assert "account" in response.json()["detail"].lower()


def test_transactions_default_to_the_main_account(client):
    add_tx(client, "5.00")
    assert client.get("/api/transactions").json()["items"][0]["account"] == "Main"


def test_filter_transactions_by_account(client):
    create_account(client, "Wallet")
    add_tx(client, "5.00", account="Main")
    add_tx(client, "7.00", account="Wallet")
    items = client.get("/api/transactions?account=Wallet").json()["items"]
    assert len(items) == 1 and items[0]["account"] == "Wallet"


def test_transaction_list_exposes_accounts_in_use(client):
    add_tx(client, "5.00", account="Main")
    body = client.get("/api/transactions").json()
    assert body["accounts"] == ["Main"]


def test_search_matches_account_name(client):
    create_account(client, "Wallet")
    add_tx(client, "5.00", account="Wallet", category="Groceries")
    found = client.get("/api/transactions?search=Wallet").json()["total"]
    assert found == 1


def test_import_creates_accounts_before_validating_transactions(client):
    payload = {
        "replace": False,
        "accounts": [{"name": "Brokerage", "kind": "investment", "opening_balance": "1000.00"}],
        "categories": [{"name": "Dividends", "kind": "income"}],
        "transactions": [
            {
                "amount": "25.00",
                "type": "income",
                "category": "Dividends",
                "account": "Brokerage",
                "date": "2026-09-30",
            }
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["accounts"] == 1
    assert stats["transactions"] == 1
    assert stats["skipped"] == 0
    accounts = client.get("/api/accounts").json()["accounts"]
    brokerage = next(a for a in accounts if a["name"] == "Brokerage")
    assert brokerage["opening_balance_cents"] == 100000
    assert brokerage["balance_cents"] == 102500


def test_legacy_backup_without_accounts_uses_main(client):
    payload = {
        "replace": True,
        "categories": [{"name": "Groceries", "kind": "expense"}],
        "transactions": [
            {"amount": "5.00", "type": "expense", "category": "Groceries", "date": "2026-09-30"}
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["transactions"] == 1
    assert [a["name"] for a in client.get("/api/accounts").json()["accounts"]] == ["Main"]


def test_import_skips_transaction_for_unknown_account(client):
    payload = {
        "replace": False,
        "categories": [],
        "transactions": [
            {
                "amount": "5.00",
                "type": "expense",
                "category": "Groceries",
                "account": "Ghost",
                "date": "2026-09-30",
            }
        ],
        "budgets": [],
    }
    stats = client.post("/api/import", json=payload).json()
    assert stats["transactions"] == 0 and stats["skipped"] >= 1


def test_export_includes_accounts(client):
    create_account(client, "Wallet", opening_balance="12.50")
    accounts = client.get("/api/export").json()["accounts"]
    wallet = next(a for a in accounts if a["name"] == "Wallet")
    assert wallet["opening_balance_cents"] == 1250


def test_account_export_import_round_trip_preserves_balances(client):
    create_account(client, "Wallet", kind="cash", opening_balance="10.00")
    add_tx(client, "5.00", kind="income", category="Salary", account="Wallet")
    before = client.get("/api/accounts").json()
    exported = client.get("/api/export").json()
    client.post("/api/import", json={**exported, "replace": True})
    after = client.get("/api/accounts").json()
    assert after["net_worth_cents"] == before["net_worth_cents"]
    assert after["accounts"] == before["accounts"]
