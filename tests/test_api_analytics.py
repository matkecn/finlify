"""End-to-end tests for the summary, analytics, and daily endpoints."""

from __future__ import annotations

from datetime import date

from money import from_cents
from models import Transaction


def today_iso() -> str:
    return date.today().isoformat()


def this_month_iso() -> str:
    return date.today().replace(day=1).isoformat()


def prev_month_iso() -> str:
    today = date.today()
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return date(year, month, 1).isoformat()


def add(client, amount, kind="expense", category="Groceries", when=None):
    return client.post(
        "/api/transactions",
        json={
            "amount": amount,
            "type": kind,
            "category": category,
            "date": when or today_iso(),
        },
    )


def test_summary_on_empty_database(client):
    body = client.get("/api/summary").json()
    assert body["income_cents"] == 0
    assert body["expenses_cents"] == 0
    assert body["balance_cents"] == 0
    assert body["savings_rate"] == 0.0
    assert body["transaction_count"] == 0
    assert body["largest_expense"] is None
    assert body["top_category"] is None
    assert body["biggest_jump"] is None
    assert body["budgets"] == []


def test_summary_totals_are_exact_cents(client):
    add(client, "0.10", category="Groceries")
    add(client, "2.675", category="Dining")
    add(client, "1000.00", kind="income", category="Salary")
    body = client.get("/api/summary").json()
    assert body["expenses_cents"] == 10 + 268
    assert body["income_cents"] == 100000
    assert body["balance_cents"] == 100000 - 278


def test_balance_equals_income_minus_expenses(client):
    add(client, "10.00", category="Groceries")
    add(client, "25.00", kind="income", category="Salary")
    body = client.get("/api/summary").json()
    assert body["balance_cents"] == body["income_cents"] - body["expenses_cents"]


def test_savings_rate(client):
    add(client, "25.00", kind="income", category="Salary")
    add(client, "10.00", category="Groceries")
    body = client.get("/api/summary").json()
    assert body["savings_rate"] == 60.0


def test_unclassified_rows_are_excluded_from_totals_but_counted(client, db):
    db.add(
        Transaction(amount_cents=9999, type="string", category="Other", date=date.today())
    )
    db.commit()
    body = client.get("/api/summary").json()
    assert body["income_cents"] == 0
    assert body["expenses_cents"] == 0
    assert body["balance_cents"] == 0
    assert body["transaction_count"] == 1
    assert body["classified_count"] == 0
    assert body["unclassified_count"] == 1


def test_summary_counts_classified_and_unclassified(client, db):
    add(client, "5.00", category="Groceries")
    db.add(Transaction(amount_cents=1, type="string", category="Other", date=date.today()))
    db.commit()
    body = client.get("/api/summary").json()
    assert body["transaction_count"] == 2
    assert body["classified_count"] == 1
    assert body["unclassified_count"] == 1


def test_month_block_matches_all_time_for_a_single_month(client):
    add(client, "12.00", category="Groceries")
    add(client, "30.00", kind="income", category="Salary")
    body = client.get("/api/summary").json()
    assert body["month"]["income_cents"] == body["income_cents"]
    assert body["month"]["expenses_cents"] == body["expenses_cents"]


def test_projected_and_average_spend_are_consistent(client):
    add(client, "20.00", category="Groceries")
    month = client.get("/api/summary").json()["month"]
    expected_projected = from_cents(
        round(month["expenses_cents"] / month["days_elapsed"]) * month["days_in_month"]
    )
    expected_daily = from_cents(round(month["expenses_cents"] / month["days_elapsed"]))
    assert month["projected_expenses"] == expected_projected
    assert month["avg_daily_spend"] == expected_daily


def test_largest_expense(client):
    add(client, "5.00", category="Groceries")
    add(client, "50.00", category="Dining")
    add(client, "20.00", category="Travel")
    largest = client.get("/api/summary").json()["largest_expense"]
    assert largest["amount_cents"] == 5000 and largest["category"] == "Dining"


def test_top_category_of_the_month(client):
    add(client, "5.00", category="Groceries")
    add(client, "50.00", category="Dining", when=this_month_iso())
    top = client.get("/api/summary").json()["top_category"]
    assert top["category"] == "Dining"


def test_biggest_jump_compares_to_previous_month(client):
    add(client, "10.00", category="Dining", when=prev_month_iso())
    add(client, "40.00", category="Dining", when=this_month_iso())
    jump = client.get("/api/summary").json()["biggest_jump"]
    assert jump["category"] == "Dining"
    assert jump["delta_cents"] == 3000


def test_timeseries_bucket_count(client):
    body = client.get("/api/timeseries?months=6").json()
    assert body["months"] == 6
    assert len(body["series"]) == 6


def test_timeseries_buckets_are_zero_filled(client):
    series = client.get("/api/timeseries?months=6").json()["series"]
    assert all(b["income"] == 0.0 and b["expenses"] == 0.0 and b["count"] == 0 for b in series)
    assert all({"month", "label", "year", "income", "expenses", "net", "count"} <= set(b) for b in series)


def test_timeseries_includes_this_month(client):
    add(client, "20.00", category="Groceries", when=this_month_iso())
    series = client.get("/api/timeseries?months=3").json()["series"]
    assert series[-1]["expenses"] == 20.0
    assert series[-1]["count"] == 1


def test_timeseries_rejects_out_of_range_months(client):
    assert client.get("/api/timeseries?months=0").status_code == 422
    assert client.get("/api/timeseries?months=37").status_code == 422


def test_breakdown_orders_by_total_descending(client):
    add(client, "30.00", category="Dining")
    add(client, "10.00", category="Groceries")
    add(client, "50.00", kind="income", category="Salary")
    body = client.get("/api/breakdown?months=1&type=expense").json()
    names = [c["category"] for c in body["categories"]]
    assert names == ["Dining", "Groceries"]


def test_breakdown_percentages_sum_to_about_one_hundred(client):
    add(client, "30.00", category="Dining")
    add(client, "10.00", category="Groceries")
    body = client.get("/api/breakdown?months=1&type=expense").json()
    total = sum(c["pct"] for c in body["categories"])
    assert 99.9 <= total <= 100.1


def test_breakdown_carries_color_and_counts(client):
    add(client, "30.00", category="Dining")
    add(client, "5.00", category="Dining")
    entry = client.get("/api/breakdown?months=1&type=expense").json()["categories"][0]
    assert entry["count"] == 2 and entry["total_cents"] == 3500
    assert entry["color"].startswith("#")


def test_breakdown_can_filter_income(client):
    add(client, "50.00", kind="income", category="Salary")
    add(client, "30.00", category="Dining")
    body = client.get("/api/breakdown?months=1&type=income").json()
    assert [c["category"] for c in body["categories"]] == ["Salary"]


def test_breakdown_rejects_bad_type(client):
    assert client.get("/api/breakdown?type=gift").status_code == 422


def test_daily_bucket_count(client):
    body = client.get("/api/daily?days=30").json()
    assert body["days"] == 30 and len(body["series"]) == 30


def test_daily_includes_today(client):
    add(client, "7.00", category="Groceries")
    series = client.get("/api/daily?days=7").json()["series"]
    assert series[-1]["date"] == today_iso()
    assert series[-1]["expenses"] == 7.0


def test_daily_rejects_out_of_range_days(client):
    assert client.get("/api/daily?days=6").status_code == 422
    assert client.get("/api/daily?days=181").status_code == 422


def test_dashboard_alias_matches_summary(client):
    add(client, "10.00", category="Groceries")
    legacy = client.get("/dashboard").json()
    summary = client.get("/api/summary").json()
    assert legacy["income"] == summary["income"]
    assert legacy["expenses"] == summary["expenses"]
    assert legacy["transaction_count"] == summary["transaction_count"]


def test_health_counts_rows(client):
    add(client, "10.00", category="Groceries")
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["transactions"] == 1
    assert body["categories"] == 19
    assert body["budgets"] == 0
