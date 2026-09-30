"""Tests for local sign-in, account setup, and per-user data isolation."""

from __future__ import annotations

from conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    SECOND_PASSWORD,
    SECOND_USERNAME,
)

import auth
from models import Account, AuthSession, Category, Transaction, User


def add(client, amount, kind="expense", category="Groceries", date="2026-09-30", **extra):
    body = {"amount": amount, "type": kind, "category": category, "date": date}
    body.update(extra)
    return client.post("/api/transactions", json=body)


def test_health_is_public_and_reports_state(signed_out):
    body = signed_out.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["users"] == 1
    assert body["needs_setup"] is False


def test_health_still_works_before_anyone_claims_the_ledger(client):
    client.cookies.clear()
    client.post("/api/auth/logout")
    assert signed_out_setup_is_refused(client)


def signed_out_setup_is_refused(client):
    return client.get("/api/accounts").status_code == 401


def test_every_data_endpoint_requires_a_session(signed_out):
    for method, path in [
        ("get", "/api/summary"),
        ("get", "/api/accounts"),
        ("get", "/api/categories"),
        ("get", "/api/budgets"),
        ("get", "/api/transactions"),
        ("get", "/api/timeseries"),
        ("get", "/api/breakdown"),
        ("get", "/api/daily"),
        ("get", "/api/export"),
        ("get", "/api/settings"),
        ("get", "/api/users"),
        ("get", "/dashboard"),
    ]:
        assert getattr(signed_out, method)(path).status_code == 401, path


def test_writes_require_a_session(signed_out):
    assert add(signed_out, "5.00").status_code == 401
    assert signed_out.post("/api/accounts", json={"name": "Cash"}).status_code == 401
    assert signed_out.post("/api/categories", json={"name": "Pets"}).status_code == 401
    assert signed_out.put("/api/budgets/Groceries", json={"limit": 10}).status_code == 401
    assert signed_out.post("/api/import", json={"replace": True}).status_code == 401
    assert signed_out.put("/api/settings", json={"currency": "GBP"}).status_code == 401


def test_the_owner_session_is_signed_in_by_the_fixture(client):
    body = client.get("/api/settings").json()
    assert body["user"]["username"] == ADMIN_USERNAME
    assert body["user"]["is_admin"] is True
    assert client.get("/api/auth/state").json()["authenticated"] is True


def test_auth_state_is_public_and_lists_options(signed_out):
    body = signed_out.get("/api/auth/state").json()
    assert body["authenticated"] is False
    assert body["needs_setup"] is False
    assert body["user"] is None
    assert body["themes"] == ["dark", "light", "auto"]
    codes = {c["code"] for c in body["currencies"]}
    assert {"USD", "EUR", "GBP", "PLN", "JPY"} <= codes
    assert all({"code", "symbol", "label"} <= set(c) for c in body["currencies"])


def test_a_claimed_ledger_cannot_be_claimed_again(client):
    response = client.post(
        "/api/auth/setup",
        json={"username": "someone-else", "password": "another-password"},
    )
    assert response.status_code == 409


def test_setup_refuses_to_overwrite_an_existing_admin(client, db):
    admin = db.query(User).filter(User.is_admin.is_(True)).one()
    assert admin.has_password
    response = client.post(
        "/api/auth/setup", json={"username": "x", "password": "y" * 12}
    )
    assert response.status_code == 409


def test_login_rejects_a_wrong_password_without_revealing_the_user(client, sign_in_as):
    client.post("/api/auth/logout")
    assert client.get("/api/summary").status_code == 401

    response = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "wrong"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect username or password"


def test_login_for_an_unknown_user_looks_identical(client, sign_in_as):
    client.post("/api/auth/logout")
    unknown = client.post(
        "/api/auth/login", json={"username": "ghost", "password": "some-password"}
    )
    wrong = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": "some-password"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_login_succeeds_with_the_right_password(client, sign_in_as):
    client.post("/api/auth/logout")
    sign_in_as(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert client.get("/api/summary").status_code == 200


def test_logout_invalidates_the_session(client, db):
    client.post("/api/auth/logout")
    assert client.get("/api/summary").status_code == 401
    assert db.query(AuthSession).count() == 0


def test_the_session_cookie_is_locked_down(client):
    header = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.lower().replace("samesite=lax", "SameSite=lax")
    assert "Path=/" in header


def test_passwords_are_salted_hashes_not_plaintext(db):
    admin = db.query(User).filter(User.is_admin.is_(True)).one()
    assert admin.password_hash != ADMIN_PASSWORD
    assert auth.verify_password(ADMIN_PASSWORD, admin.password_hash)
    assert not auth.verify_password(ADMIN_PASSWORD + "x", admin.password_hash)


def test_two_users_with_the_same_password_get_different_hashes():
    first = auth.hash_password("identical-password")
    second = auth.hash_password("identical-password")
    assert first != second
    assert auth.verify_password("identical-password", first)
    assert auth.verify_password("identical-password", second)


def test_registration_creates_an_isolated_ledger(client, other_user):
    assert other_user["username"] == SECOND_USERNAME
    assert other_user["is_admin"] is False
    assert other_user["id"] != 1

    body = client.get("/api/summary").json()
    assert body["balance_cents"] == 0
    assert body["transaction_count"] == 0
    assert [a["name"] for a in client.get("/api/accounts").json()["accounts"]] == ["Main"]
    assert len(client.get("/api/categories").json()["categories"]) == 19
    assert client.get("/api/budgets").json()["budgets"] == []


def test_a_new_user_starts_with_their_own_ledger(db, other_user):
    user = db.query(User).filter(User.username == SECOND_USERNAME).one()
    assert db.query(Category).filter(Category.user_id == user.id).count() == 19
    assert db.query(Account).filter(Account.user_id == user.id).count() == 1


def test_users_cannot_read_each_others_transactions(client, other_user, sign_in_as):
    created = add(client, "42.00", description="mine").json()
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)

    items = client.get("/api/transactions").json()["items"]
    assert items == []
    assert client.put(
        f"/api/transactions/{created['id']}",
        json={"amount": 1, "type": "expense", "category": "Groceries", "date": "2026-09-30"},
    ).status_code == 404
    assert client.delete(f"/api/transactions/{created['id']}").status_code == 404


def test_users_cannot_touch_each_others_accounts(client, other_user, sign_in_as, db):
    account_id = db.query(Account).order_by(Account.id).first().id
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)

    assert client.put(
        f"/api/accounts/{account_id}", json={"name": "Hijack", "kind": "checking"}
    ).status_code == 404
    assert client.delete(f"/api/accounts/{account_id}").status_code == 404


def test_users_cannot_touch_each_others_budgets(client, other_user, sign_in_as):
    client.put("/api/budgets/Groceries", json={"limit": 100})
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)

    assert client.get("/api/budgets").json()["budgets"] == []
    assert client.put("/api/budgets/Groceries", json={"limit": 5}).status_code == 200
    assert client.get("/api/budgets").json()["budgets"][0]["limit_cents"] == 500

    client.post("/api/auth/logout")
    sign_in_as(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert client.get("/api/budgets").json()["budgets"][0]["limit_cents"] == 10000


def test_users_cannot_delete_each_others_categories(client, other_user, sign_in_as, db):
    category_id = db.query(Category).order_by(Category.id).first().id
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)
    assert client.delete(f"/api/categories/{category_id}").status_code == 404


def test_settings_update_currency_and_theme(client):
    body = client.put("/api/settings", json={"currency": "gbp", "theme": "light"}).json()
    assert body["user"]["currency"] == "GBP"
    assert body["user"]["theme"] == "light"
    assert client.get("/api/settings").json()["user"]["currency"] == "GBP"


def test_settings_accept_currency_and_theme_everywhere(client):
    codes = {c["code"] for c in client.get("/api/settings").json()["currencies"]}
    for code in codes:
        assert client.put("/api/settings", json={"currency": code}).status_code == 200
    for theme in ("dark", "light", "auto"):
        assert client.put("/api/settings", json={"theme": theme}).status_code == 200


def test_settings_reject_an_unknown_currency_or_theme(client):
    assert client.put("/api/settings", json={"currency": "XYZ"}).status_code == 422
    assert client.put("/api/settings", json={"currency": "US"}).status_code == 422
    assert client.put("/api/settings", json={"theme": "neon"}).status_code == 422


def test_settings_update_the_display_name(client):
    assert client.put("/api/settings", json={"display_name": "Sam Sr."}).json()["user"][
        "display_name"
    ] == "Sam Sr."


def test_settings_are_private_to_each_user(client, other_user, sign_in_as):
    client.put("/api/settings", json={"currency": "CHF"})
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)
    assert client.get("/api/settings").json()["user"]["currency"] == "EUR"


def test_changing_a_password_requires_the_current_one(client):
    response = client.post(
        "/api/settings/password",
        json={"current_password": "not-it", "new_password": "a-new-password"},
    )
    assert response.status_code == 401


def test_changing_a_password_ends_the_other_sessions(client, sign_in_as, db):
    client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "brand-new-password"},
    )
    assert db.query(AuthSession).count() == 1
    client.post("/api/auth/logout")
    assert client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).status_code == 401
    sign_in_as(ADMIN_USERNAME, "brand-new-password")
    assert client.get("/api/summary").status_code == 200


def test_a_weak_password_is_refused(client):
    response = client.post(
        "/api/settings/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "short"},
    )
    assert response.status_code == 422


def test_only_the_administrator_may_manage_users(client, other_user, sign_in_as):
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)
    assert client.get("/api/users").status_code == 403
    assert client.post(
        "/api/users", json={"username": "sneaky", "password": "sneaky-password"}
    ).status_code == 403
    assert client.delete("/api/users/1").status_code == 403


def test_the_administrator_can_create_and_edit_users(client):
    created = client.post(
        "/api/users",
        json={"username": "Robin", "password": "robin-password", "display_name": "Rob"},
    )
    assert created.status_code == 201
    assert created.json()["username"] == "robin"
    assert created.json()["is_admin"] is False

    patched = client.put(
        f"/api/users/{created.json()['id']}", json={"currency": "SEK", "theme": "auto"}
    ).json()
    assert patched["currency"] == "SEK"
    assert patched["theme"] == "auto"


def test_usernames_are_unique_whatever_their_case(client):
    assert client.post(
        "/api/users", json={"username": "owner", "password": "some-password"}
    ).status_code == 409
    assert client.post(
        "/api/users", json={"username": "OWNER", "password": "some-password"}
    ).status_code == 409


def test_the_administrator_can_reset_any_password(client, other_user, sign_in_as):
    user_id = other_user["id"]
    assert user_id != 1
    assert client.post(
        f"/api/users/{user_id}/password", json={"password": "reset-password"}
    ).status_code == 200
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, "reset-password")
    assert client.get("/api/settings").status_code == 200


def test_the_administrator_cannot_be_deleted(client):
    assert client.delete("/api/users/1").status_code == 409
    assert client.get("/api/users").json()["users"][0]["is_admin"] is True


def test_deleting_a_user_takes_their_ledger_with_it(client, other_user, sign_in_as, db):
    add(client, "10.00")
    other = db.query(User).filter(User.username == SECOND_USERNAME).one()
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)
    add(client, "3.00")
    assert db.query(Transaction).filter(Transaction.user_id == other.id).count() == 1

    client.post("/api/auth/logout")
    sign_in_as(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert db.query(Transaction).filter(Transaction.user_id == other.id).count() == 1
    assert client.delete(f"/api/users/{other.id}").status_code == 200

    assert db.query(User).filter(User.id == other.id).first() is None
    assert db.query(Transaction).filter(Transaction.user_id == other.id).count() == 0
    assert db.query(Category).filter(Category.user_id == other.id).count() == 0
    assert db.query(Account).filter(Account.user_id == other.id).count() == 0
    assert client.get("/api/summary").json()["transaction_count"] == 1


def test_a_deleted_user_cannot_sign_in(client, other_user):
    client.delete(f"/api/users/{other_user['id']}")
    client.post("/api/auth/logout")
    assert client.post(
        "/api/auth/login", json={"username": SECOND_USERNAME, "password": SECOND_PASSWORD}
    ).status_code == 401


def test_deleting_an_unknown_user_is_a_404(client):
    assert client.delete("/api/users/9999").status_code == 404


def test_export_only_contains_the_signed_in_users_rows(client, other_user, sign_in_as):
    add(client, "7.50")
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)
    add(client, "2.25")

    body = client.get("/api/export").json()
    assert body["owner"] == SECOND_USERNAME
    assert len(body["transactions"]) == 1
    assert body["transactions"][0]["amount_cents"] == 225


def test_importing_cannot_touch_another_users_rows(client, other_user, sign_in_as, db):
    stolen = client.get("/api/export").json()
    assert add(client, "5.00", description="mine").status_code == 201

    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)
    client.post("/api/import", json={**stolen, "replace": True})

    assert client.get("/api/summary").json()["transaction_count"] == 0
    other = db.query(User).filter(User.username == ADMIN_USERNAME).one()
    assert db.query(Transaction).filter(Transaction.user_id == other.id).count() == 1


def test_expired_sessions_are_purged(db, client):
    import time

    client.post("/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    for row in db.query(AuthSession).all():
        row.expires_at = int(time.time()) - 10
    db.commit()

    assert client.get("/api/summary").status_code == 401
    client.post("/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    client.get("/api/auth/state")
    assert db.query(AuthSession).filter(AuthSession.expires_at > 0).count() == 1


def test_a_forged_cookie_is_ignored(client):
    client.cookies.clear()
    client.cookies.set("finlify_session", "not-a-real-token")
    assert client.get("/api/summary").status_code == 401
