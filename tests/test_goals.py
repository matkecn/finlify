"""End-to-end tests for savings goals and their live progress."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import SECOND_PASSWORD, SECOND_USERNAME
from models import Transaction


def add_tx(client, amount, kind="expense", category="Groceries", account="Main", when=None):
    """Post one transaction and assert it was accepted."""
    response = client.post(
        "/api/transactions",
        json={
            "amount": amount,
            "type": kind,
            "category": category,
            "account": account,
            "date": (when or date.today()).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create(client, name="Holiday", **fields):
    """Create a goal with a sensible default target."""
    payload = {"name": name, "target": "1000.00"}
    payload.update(fields)
    response = client.post("/api/goals", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def goals(client, **params):
    query = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    response = client.get(f"/api/goals{query}")
    assert response.status_code == 200, response.text
    return response.json()["goals"]


def test_new_ledger_has_no_goals(client):
    assert goals(client) == []


def test_created_goal_comes_back_with_progress(client):
    row = create(client, "Holiday", target="1000.00")
    assert row["name"] == "Holiday"
    assert row["target_cents"] == 100000
    assert row["saved_cents"] == 0
    assert row["remaining_cents"] == 100000
    assert row["pct"] == 0.0
    assert row["state"] == "saving"
    assert row["color"]


def test_goal_measures_the_whole_ledger_when_it_names_no_account(client):
    client.put("/api/accounts/1", json={"opening_balance": "400.00"})
    row = create(client, "Rainy day", target="1000.00")
    assert row["saved_cents"] == 40000
    assert row["pct"] == 40.0
    assert row["remaining_cents"] == 60000


def test_goal_measures_only_its_account(client):
    client.put("/api/accounts/1", json={"opening_balance": "900.00"})
    savings = client.post("/api/accounts", json={"name": "Savings", "opening_balance": "250.00"}).json()
    row = create(client, "Car", target="500.00", account="Savings")
    assert row["saved_cents"] == savings["balance_cents"] == 25000
    assert row["pct"] == 50.0


def test_progress_follows_new_transactions(client):
    client.put("/api/accounts/1", json={"opening_balance": "250.00"})
    create(client, "Emergency", target="1000.00")
    add_tx(client, "100.00", kind="income")
    assert goals(client)[0]["saved_cents"] == 35000
    assert goals(client)[0]["pct"] == 35.0


def test_spending_against_a_goal_account_lowers_its_progress(client):
    client.post("/api/accounts", json={"name": "Savings", "opening_balance": "300.00"})
    create(client, "Car", target="1000.00", account="Savings")
    add_tx(client, "50.00", account="Savings")
    row = goals(client)[0]
    assert row["saved_cents"] == 25000
    assert row["pct"] == 25.0
    assert row["remaining_cents"] == 75000


def test_target_reached_marks_the_goal_reached(client):
    client.put("/api/accounts/1", json={"opening_balance": "1200.00"})
    row = create(client, "Done already", target="1000.00")
    assert row["state"] == "reached"
    assert row["pct"] == 120.0
    assert row["remaining_cents"] == 0


def test_past_deadline_without_the_target_is_overdue(client):
    row = create(client, "Late", target="1000.00", deadline="2020-01-01")
    assert row["state"] == "overdue"
    assert row["days_left"] < 0
    assert row["needed_per_month"] is None


def test_future_deadline_reports_days_left_and_a_pace(client):
    when = date.today() + timedelta(days=60)
    row = create(client, "Trip", target="1200.00", deadline=when.isoformat())
    assert row["state"] == "saving"
    assert row["days_left"] == 60
    assert row["needed_per_month"] == 600.0


def test_reached_goal_beats_overdue_deadline(client):
    client.put("/api/accounts/1", json={"opening_balance": "1500.00"})
    row = create(client, "Late but won", target="1000.00", deadline="2020-01-01")
    assert row["state"] == "reached"


def test_duplicate_goal_names_are_rejected(client):
    create(client, "Holiday")
    again = client.post("/api/goals", json={"name": "holiday", "target": "500.00"})
    assert again.status_code == 409


def test_target_must_be_positive(client):
    assert client.post("/api/goals", json={"name": "Zero", "target": "0"}).status_code == 422
    assert client.post("/api/goals", json={"name": "Neg", "target": "-5.00"}).status_code == 422


def test_target_rounds_the_same_way_transaction_amounts_do(client):
    # Goals reuse the shared amount parser, so a target of 10.005 lands on the
    # same cent a 10.005 transaction would.
    row = create(client, "Precise", target="10.005")
    assert row["target_cents"] == 1001


def test_blank_name_is_rejected(client):
    assert client.post("/api/goals", json={"name": "   ", "target": "10"}).status_code == 422


def test_unknown_account_is_rejected(client):
    response = client.post("/api/goals", json={"name": "Ghost", "target": "10", "account": "Nope"})
    assert response.status_code == 422


def test_blank_account_falls_back_to_the_whole_ledger(client):
    client.put("/api/accounts/1", json={"opening_balance": "600.00"})
    row = create(client, "Blank link", target="1000.00", account="   ")
    assert row["account"] is None
    assert row["saved_cents"] == 60000


def test_updating_a_goal_recalculates_its_progress(client):
    client.put("/api/accounts/1", json={"opening_balance": "500.00"})
    row = create(client, "Fund", target="1000.00")
    updated = client.put(
        f"/api/goals/{row['id']}",
        json={"name": "Bigger fund", "target": "2000.00"},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "Bigger fund"
    assert body["target_cents"] == 200000
    assert body["pct"] == 25.0


def test_updating_a_goal_rejects_a_duplicate_name(client):
    first = create(client, "One")
    create(client, "Two")
    response = client.put(f"/api/goals/{first['id']}", json={"name": "two", "target": "10.00"})
    assert response.status_code == 409


def test_renaming_a_goal_to_its_own_name_is_allowed(client):
    row = create(client, "Same")
    response = client.put(f"/api/goals/{row['id']}", json={"name": "Same", "target": "10.00"})
    assert response.status_code == 200


def test_deleting_a_goal_removes_it(client):
    row = create(client)
    assert client.delete(f"/api/goals/{row['id']}").json() == {"deleted": "Holiday"}
    assert goals(client) == []


def test_archived_goals_are_hidden_until_asked_for(client):
    row = create(client, "Old goal", is_archived=True)
    assert goals(client) == []
    assert [g["id"] for g in goals(client, include_archived=True)] == [row["id"]]


def test_goals_are_ordered_by_sort_order(client):
    second = create(client, "Second", sort_order=2)
    first = create(client, "First", sort_order=1)
    assert [g["id"] for g in goals(client)] == [first["id"], second["id"]]


def test_goals_require_a_session(signed_out):
    assert signed_out.get("/api/goals").status_code == 401
    assert signed_out.post("/api/goals", json={"name": "X", "target": "1"}).status_code == 401


def test_goals_are_scoped_to_their_owner(client, other_user, sign_in_as):
    row = create(client, "Mine")
    client.post("/api/auth/logout")
    sign_in_as(SECOND_USERNAME, SECOND_PASSWORD)
    assert client.get("/api/goals").json()["goals"] == []
    assert client.put(
        f"/api/goals/{row['id']}", json={"name": "Theirs", "target": "1.00"}
    ).status_code == 404
    assert client.delete(f"/api/goals/{row['id']}").status_code == 404


def test_renaming_an_account_keeps_its_goal_attached(client):
    savings = client.post("/api/accounts", json={"name": "Savings", "opening_balance": "100.00"}).json()
    row = create(client, "Rainy day", target="500.00", account="Savings")
    client.put(f"/api/accounts/{savings['id']}", json={"name": "Emergency fund"})
    assert goals(client)[0]["account"] == "Emergency fund"
    assert goals(client)[0]["saved_cents"] == 10000


def test_deleting_an_account_tracked_by_a_goal_is_refused(client):
    savings = client.post("/api/accounts", json={"name": "Savings", "opening_balance": "100.00"}).json()
    create(client, "Rainy day", account="Savings")
    response = client.delete(f"/api/accounts/{savings['id']}")
    assert response.status_code == 409
    assert "goal" in response.json()["detail"]


def test_goal_progress_ignores_unclassified_money(client, db):
    db.add(Transaction(amount_cents=5000, type="string", category="Other", date=date.today()))
    db.commit()
    assert goals(client) == []
    row = create(client, "Anything", target="100.00")
    assert row["saved_cents"] == 0

def test_goal_model_treats_a_blank_account_as_absent(client, db):
    """The model itself must survive blank input, not just the API layer."""
    from models import Goal

    row = Goal(name="Blank", target_cents=1000, account="   ", user_id=1)
    db.add(row)
    db.commit()
    assert row.account is None
