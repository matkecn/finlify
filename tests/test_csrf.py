"""Tests for the same-origin guard on state-changing requests."""

from __future__ import annotations

EXPENSE = {
    "amount": "5.00",
    "type": "expense",
    "category": "Groceries",
    "date": "2026-09-30",
}


def test_cross_site_origin_is_rejected(client):
    response = client.post(
        "/api/transactions", json=EXPENSE, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403


def test_cross_site_referer_is_rejected(client):
    response = client.post(
        "/api/categories",
        json={"name": "Coffee", "kind": "expense"},
        headers={"Referer": "http://evil.example/page"},
    )
    assert response.status_code == 403


def test_origin_is_checked_when_a_referer_is_also_present(client):
    response = client.post(
        "/api/transactions",
        json=EXPENSE,
        headers={"Origin": "http://evil.example", "Referer": "http://testserver/"},
    )
    assert response.status_code == 403


def test_same_origin_is_allowed(client):
    response = client.post(
        "/api/transactions", json=EXPENSE, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 201


def test_absent_origin_is_allowed(client):
    assert client.post("/api/transactions", json=EXPENSE).status_code == 201


def test_null_origin_is_rejected(client):
    response = client.post(
        "/api/transactions", json=EXPENSE, headers={"Origin": "null"}
    )
    assert response.status_code == 403


def test_safe_methods_ignore_origin(client):
    response = client.get(
        "/api/transactions", headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 200


def test_delete_is_guarded(client):
    created = client.post("/api/transactions", json=EXPENSE).json()
    response = client.delete(
        f"/api/transactions/{created['id']}", headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
