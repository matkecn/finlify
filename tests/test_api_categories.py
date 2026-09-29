"""End-to-end tests for the category endpoints."""

from __future__ import annotations

import pytest

from models import CATEGORY_PALETTE


def create(client, name, kind="expense", color=None, is_archived=False):
    body = {"name": name, "kind": kind}
    if color is not None:
        body["color"] = color
    if is_archived:
        body["is_archived"] = True
    return client.post("/api/categories", json=body)


def test_default_categories_are_seeded(client):
    body = client.get("/api/categories").json()
    assert len(body["categories"]) == 19
    names = {c["name"] for c in body["categories"]}
    assert {"Groceries", "Salary", "Other", "Other income"} <= names


def test_create_category(client):
    response = create(client, "Coffee")
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Coffee"
    assert body["kind"] == "expense"
    assert body["color"] in CATEGORY_PALETTE
    assert body["is_archived"] is False


def test_create_category_with_explicit_colour(client):
    assert create(client, "Coffee", color="#c084fc").json()["color"] == "#c084fc"


def test_category_name_is_normalised_on_create(client):
    assert create(client, "  cold   brew ").json()["name"] == "Cold brew"


def test_duplicate_category_is_rejected(client):
    create(client, "Coffee")
    assert create(client, "Coffee").status_code == 409


def test_invalid_kind_is_rejected(client):
    assert create(client, "Coffee", kind="crypto").status_code == 422


def test_blank_name_is_rejected(client):
    assert create(client, "   ").status_code == 422


def test_invalid_colour_is_rejected(client):
    assert create(client, "Coffee", color="teal").status_code == 422


def test_categories_are_grouped_by_kind(client):
    body = client.get("/api/categories").json()["categories"]
    kinds = [c["kind"] for c in body]
    assert kinds == sorted(kinds, key=lambda k: {"expense": 0, "income": 1}[k])


def test_rename_cascades_to_transactions(client):
    category = create(client, "Coffee").json()
    client.post(
        "/api/transactions",
        json={"amount": "4", "type": "expense", "category": "Coffee", "date": "2026-09-30"},
    )
    client.put(f"/api/categories/{category['id']}", json={"name": "Espresso"})
    body = client.get("/api/transactions?search=Espresso").json()
    assert body["total"] == 1
    assert body["items"][0]["category"] == "Espresso"


def test_rename_cascades_to_budgets(client):
    category = create(client, "Coffee").json()
    client.put("/api/budgets/Coffee", json={"limit": "20"})
    client.put(f"/api/categories/{category['id']}", json={"name": "Espresso"})
    budgets = client.get("/api/budgets").json()["budgets"]
    assert [b["category"] for b in budgets] == ["Espresso"]


def test_rename_collision_is_rejected(client):
    create(client, "Coffee")
    other = create(client, "Tea").json()
    assert client.put(f"/api/categories/{other['id']}", json={"name": "Coffee"}).status_code == 409


def test_rename_to_blank_is_rejected(client):
    category = create(client, "Coffee").json()
    assert client.put(f"/api/categories/{category['id']}", json={"name": "  "}).status_code == 422


def test_partial_update_keeps_other_fields(client):
    category = create(client, "Coffee").json()
    body = client.put(f"/api/categories/{category['id']}", json={"color": "#fbbf24"}).json()
    assert body["color"] == "#fbbf24"
    assert body["name"] == "Coffee" and body["kind"] == "expense"


def test_update_kind(client):
    category = create(client, "Coffee").json()
    body = client.put(f"/api/categories/{category['id']}", json={"kind": "income"}).json()
    assert body["kind"] == "income"


def test_archive_hides_category_from_default_list(client):
    category = create(client, "Coffee").json()
    client.put(f"/api/categories/{category['id']}", json={"is_archived": True})
    names = {c["name"] for c in client.get("/api/categories").json()["categories"]}
    assert "Coffee" not in names


def test_archived_category_appears_on_request(client):
    category = create(client, "Coffee").json()
    client.put(f"/api/categories/{category['id']}", json={"is_archived": True})
    body = client.get("/api/categories?include_archived=true").json()
    assert any(c["name"] == "Coffee" for c in body["categories"])


def test_archived_category_still_accepts_new_transactions(client):
    category = create(client, "Coffee").json()
    client.put(f"/api/categories/{category['id']}", json={"is_archived": True})
    response = client.post(
        "/api/transactions",
        json={"amount": "4", "type": "expense", "category": "Coffee", "date": "2026-09-30"},
    )
    assert response.status_code == 201


def test_delete_is_blocked_while_in_use(client):
    category = create(client, "Coffee").json()
    client.post(
        "/api/transactions",
        json={"amount": "4", "type": "expense", "category": "Coffee", "date": "2026-09-30"},
    )
    response = client.delete(f"/api/categories/{category['id']}")
    assert response.status_code == 409
    assert "archive" in response.json()["detail"].lower()


def test_delete_unused_category(client):
    category = create(client, "Coffee").json()
    assert client.delete(f"/api/categories/{category['id']}").json() == {"deleted": "Coffee"}
    names = {c["name"] for c in client.get("/api/categories").json()["categories"]}
    assert "Coffee" not in names


def test_delete_removes_attached_budget(client):
    category = create(client, "Coffee").json()
    client.put("/api/budgets/Coffee", json={"limit": "20"})
    client.delete(f"/api/categories/{category['id']}")
    assert client.get("/api/budgets").json()["budgets"] == []


def test_delete_missing_category_returns_404(client):
    assert client.delete("/api/categories/999999").status_code == 404


def test_update_missing_category_returns_404(client):
    assert client.put("/api/categories/999999", json={"name": "X"}).status_code == 404


@pytest.mark.parametrize("name", ["Coffee", "Rent", "Side project"])
def test_created_categories_are_retrievable(client, name):
    create(client, name)
    names = {c["name"] for c in client.get("/api/categories").json()["categories"]}
    assert name in names
