"""End-to-end tests for the net worth history endpoint."""

from __future__ import annotations

from datetime import date, timedelta

from money import from_cents
from models import Transaction


def today() -> date:
    return date.today()


def add(client, amount, kind="expense", category="Groceries", when=None, account="Main"):
    """Post one transaction and assert it was accepted."""
    response = client.post(
        "/api/transactions",
        json={
            "amount": amount,
            "type": kind,
            "category": category,
            "account": account,
            "date": (when or today()).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def series(client, months=6):
    response = client.get(f"/api/networth?months={months}")
    assert response.status_code == 200, response.text
    return response.json()


def test_net_worth_starts_at_the_opening_balance(client):
    client.put(
        "/api/accounts/1",
        json={"opening_balance": "1000.00"},
    )
    body = series(client)
    assert body["months"] == 6
    assert len(body["series"]) == 6
    assert body["series"][0]["net_worth"] == 1000.0
    assert body["series"][-1]["net_worth_cents"] == 100000


def test_empty_ledger_repeats_opening_balance_every_month(client):
    client.put("/api/accounts/1", json={"opening_balance": "250.00"})
    values = [row["net_worth_cents"] for row in series(client)["series"]]
    assert values == [25000] * 6


def test_change_between_points_is_the_difference(client):
    add(client, "120.00", kind="income", when=today())
    add(client, "20.00", when=today())
    rows = series(client)["series"]
    last = rows[-1]
    assert last["net_worth_cents"] == 10000
    assert last["change_cents"] == 10000
    assert last["change"] == from_cents(10000)


def test_quiet_months_carry_the_previous_value_forward(client):
    add(client, "50.00", kind="income", when=today())
    rows = series(client, months=3)["series"]
    assert [row["net_worth_cents"] for row in rows] == [0, 0, 5000]
    assert [row["change_cents"] for row in rows] == [0, 0, 5000]


def test_a_quiet_month_between_busy_ones_stays_flat(client):
    add(client, "50.00", kind="income", when=today() - timedelta(days=70))
    rows = series(client, months=4)["series"]
    assert [row["net_worth_cents"] for row in rows] == [0, 5000, 5000, 5000]
    assert [row["change_cents"] for row in rows] == [0, 5000, 0, 0]


def test_history_before_the_window_seeds_the_first_point(client):
    add(client, "40.00", kind="income", when=today() - timedelta(days=200))
    rows = series(client, months=3)["series"]
    assert rows[0]["net_worth_cents"] == 4000
    # The change is measured against the balance entering the window, so the
    # first point reports no movement even though older money seeds it.
    assert rows[0]["change_cents"] == 0


def test_the_last_point_is_today_not_the_month_close(client):
    add(client, "75.00", kind="income", when=today())
    last = series(client)["series"][-1]
    assert last["month"] == today().strftime("%Y-%m")
    assert last["net_worth_cents"] == 7500


def test_future_dated_money_is_not_counted_yet(client):
    tomorrow = today() + timedelta(days=1)
    add(client, "10.00", kind="income", when=tomorrow)
    add(client, "5.00", kind="income", when=today())
    rows = series(client)["series"]
    assert rows[-1]["net_worth_cents"] == 500


def test_net_worth_spans_every_account(client):
    client.post("/api/accounts", json={"name": "Savings", "opening_balance": "300.00"})
    client.put("/api/accounts/1", json={"opening_balance": "700.00"})
    assert series(client)["series"][-1]["net_worth_cents"] == 100000


def test_archived_accounts_still_count_towards_net_worth(client):
    client.post("/api/accounts", json={"name": "Old", "opening_balance": "90.00"})
    accounts = client.get("/api/accounts").json()["accounts"]
    old = next(a for a in accounts if a["name"] == "Old")
    client.put(f"/api/accounts/{old['id']}", json={"is_archived": True})
    assert series(client)["series"][-1]["net_worth_cents"] == 9000


def test_unclassified_money_does_not_move_net_worth(client, db):
    db.add(Transaction(amount_cents=999, type="string", category="Other", date=today()))
    db.commit()
    assert series(client)["series"][-1]["net_worth_cents"] == 0


def test_months_argument_is_bounded(client):
    assert client.get("/api/networth?months=0").status_code == 422
    assert client.get("/api/networth?months=37").status_code == 422


def test_net_worth_requires_a_session(signed_out):
    assert signed_out.get("/api/networth").status_code == 401


def test_series_carries_exact_cents_for_odd_amounts(client):
    add(client, "19.99", when=today())
    add(client, "0.01", when=today())
    last = series(client)["series"][-1]
    assert last["net_worth_cents"] == -2000