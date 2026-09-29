"""End-to-end tests for the transaction endpoints."""

from __future__ import annotations

import pytest


def make(client, amount="5.00", kind="expense", category="Groceries", date="2026-09-30", description=None):
    """Create a transaction through the API and return the response."""
    body = {"amount": amount, "type": kind, "category": category, "date": date}
    if description is not None:
        body["description"] = description
    return client.post("/api/transactions", json=body)


def test_create_returns_201_and_stored_row(client):
    response = make(client, amount="4.35", description="Flat white")
    assert response.status_code == 201
    body = response.json()
    assert body["amount_cents"] == 435
    assert body["amount"] == 4.35
    assert body["type"] == "expense"
    assert body["category"] == "Groceries"
    assert body["description"] == "Flat white"
    assert body["classified"] is True
    assert body["id"] is not None


@pytest.mark.parametrize(
    "amount,cents",
    [("0.1", 10), ("2.675", 268), ("19.99", 1999), ("1000.00", 100000), ("0.01", 1)],
)
def test_created_amounts_are_exact_cents(client, amount, cents):
    assert make(client, amount=amount).json()["amount_cents"] == cents


def test_create_income(client):
    body = make(client, amount="1000.00", kind="income", category="Salary").json()
    assert body["type"] == "income" and body["amount_cents"] == 100000


@pytest.mark.parametrize("amount", ["-5", "0", "abc", "", "1,000"])
def test_invalid_amounts_are_rejected(client, amount):
    assert make(client, amount=amount).status_code == 422


def test_unknown_category_is_rejected(client):
    response = make(client, category="Nonexistent")
    assert response.status_code == 422
    assert "unknown category" in response.json()["detail"].lower()


def test_invalid_type_is_rejected(client):
    assert make(client, kind="gift").status_code == 422


@pytest.mark.parametrize("field", ["amount", "type", "category", "date"])
def test_missing_required_field_is_rejected(client, field):
    body = {"amount": "5", "type": "expense", "category": "Groceries", "date": "2026-09-30"}
    del body[field]
    assert client.post("/api/transactions", json=body).status_code == 422


def test_description_whitespace_is_collapsed(client):
    body = make(client, description="  Flat    white  ").json()
    assert body["description"] == "Flat white"


def test_empty_description_becomes_null(client):
    assert make(client, description="   ").json()["description"] is None


def test_category_whitespace_is_collapsed_and_titlecased(client):
    client.post("/api/categories", json={"name": "Coffee", "kind": "expense"})
    body = make(client, category="  coffee  ").json()
    assert body["category"] == "Coffee"


def test_overlong_description_is_rejected(client):
    assert make(client, description="x" * 200).status_code == 422


def test_list_returns_page_shape(client):
    make(client)
    body = client.get("/api/transactions").json()
    assert set(body) == {"items", "total", "limit", "offset", "has_more", "categories"}
    assert body["total"] == 1 and body["limit"] == 25 and body["offset"] == 0
    assert body["has_more"] is False


def test_list_paginates(client):
    for index in range(5):
        make(client, amount=f"{index + 1}.00", date="2026-09-30")
    first = client.get("/api/transactions?limit=2&offset=0").json()
    second = client.get("/api/transactions?limit=2&offset=2").json()
    assert len(first["items"]) == 2 and first["has_more"] is True
    assert len(second["items"]) == 2 and second["offset"] == 2


def test_list_total_reflects_all_rows_not_the_page(client):
    for _ in range(4):
        make(client)
    body = client.get("/api/transactions?limit=1").json()
    assert body["total"] == 4 and len(body["items"]) == 1


def test_filter_by_type(client):
    make(client, kind="expense")
    make(client, kind="income", category="Salary")
    body = client.get("/api/transactions?type=income").json()
    assert body["total"] == 1 and body["items"][0]["type"] == "income"


def test_filter_by_category(client):
    make(client, category="Groceries")
    make(client, category="Dining")
    body = client.get("/api/transactions?category=Dining").json()
    assert body["total"] == 1 and body["items"][0]["category"] == "Dining"


def test_search_matches_description(client):
    make(client, description="Flat white")
    make(client, description="Taxi ride")
    body = client.get("/api/transactions?search=white").json()
    assert body["total"] == 1


def test_search_matches_category(client):
    make(client, category="Groceries")
    make(client, category="Dining")
    body = client.get("/api/transactions?search=dining").json()
    assert body["total"] == 1


def test_date_range_filter(client):
    make(client, date="2026-08-01")
    make(client, date="2026-09-15")
    make(client, date="2026-10-01")
    body = client.get("/api/transactions?date_from=2026-09-01&date_to=2026-09-30").json()
    assert body["total"] == 1 and body["items"][0]["date"] == "2026-09-15"


@pytest.mark.parametrize(
    "sort,order,expected",
    [
        ("amount", "desc", 900),
        ("amount", "asc", 100),
        ("date", "desc", "2026-09-03"),
        ("date", "asc", "2026-09-01"),
        ("category", "desc", "Travel"),
        ("category", "asc", "Dining"),
    ],
)
def test_sorting(client, sort, order, expected):
    make(client, amount="1.00", category="Dining", date="2026-09-01")
    make(client, amount="9.00", category="Travel", date="2026-09-03")
    body = client.get(f"/api/transactions?sort={sort}&order={order}").json()
    first = body["items"][0]
    value = first["amount_cents"] if sort == "amount" else first[sort]
    assert value == expected


def test_update_replaces_fields(client):
    created = make(client, amount="4.35", category="Groceries", description="Old").json()
    response = client.put(
        f"/api/transactions/{created['id']}",
        json={
            "amount": "9.99",
            "type": "expense",
            "category": "Dining",
            "description": "New",
            "date": "2026-09-30",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["amount_cents"] == 999
    assert body["category"] == "Dining"
    assert body["description"] == "New"


def test_update_is_idempotent(client):
    created = make(client, amount="4.35").json()
    payload = {
        "amount": "9.99",
        "type": "expense",
        "category": "Groceries",
        "date": "2026-09-30",
    }
    first = client.put(f"/api/transactions/{created['id']}", json=payload).json()
    second = client.put(f"/api/transactions/{created['id']}", json=payload).json()
    assert first == second


def test_update_missing_returns_404(client):
    response = client.put(
        "/api/transactions/999999",
        json={"amount": "5", "type": "expense", "category": "Groceries", "date": "2026-09-30"},
    )
    assert response.status_code == 404


def test_delete_removes_the_row(client):
    created = make(client).json()
    response = client.delete(f"/api/transactions/{created['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": created["id"]}
    assert client.get("/api/transactions").json()["total"] == 0


def test_delete_missing_returns_404(client):
    assert client.delete("/api/transactions/999999").status_code == 404


def test_page_lists_categories_in_use(client):
    make(client, category="Dining")
    make(client, category="Travel")
    make(client, category="Dining")
    body = client.get("/api/transactions").json()
    assert body["categories"] == ["Dining", "Travel"]
