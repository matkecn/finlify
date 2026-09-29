"""Tests for the idempotent, on-boot database migrations."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

import migrations


def _legacy_engine(path):
    """Create a database using the pre-cents (REAL money) schema."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE transactions ("
                "id INTEGER PRIMARY KEY, amount REAL, type TEXT NOT NULL, "
                "category TEXT NOT NULL, description TEXT, date DATE NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE budgets ("
                "id INTEGER PRIMARY KEY, category TEXT NOT NULL UNIQUE, "
                "limit_amount REAL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO transactions (id, amount, type, category, description, date) VALUES "
                "(1, 4.35, 'expense', 'Coffee', 'Flat white', '2026-09-30'),"
                "(2, 0.10, 'expense', 'Coffee', NULL, '2026-09-30'),"
                "(3, 12.34, 'income', 'Salary', NULL, '2026-09-01'),"
                "(4, 100.00, 'income', 'Salary', NULL, '2026-09-01'),"
                "(5, NULL, 'string', 'Other', NULL, '2026-09-29')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO budgets (id, category, limit_amount) VALUES "
                "(1, 'Coffee', 20.0), (2, 'Groceries', 0.5), (3, 'Other', NULL)"
            )
        )
    return engine


def _column_names(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_migrates_float_money_columns_to_integer_cents(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")

    report = migrations.run(engine)

    assert report["transactions_to_cents"] is True
    assert report["budgets_to_cents"] is True
    assert report["categories"] == 19

    assert "amount_cents" in _column_names(engine, "transactions")
    assert "amount" not in _column_names(engine, "transactions")
    assert "limit_cents" in _column_names(engine, "budgets")
    assert "limit_amount" not in _column_names(engine, "budgets")


def test_migrated_transaction_amounts_are_rounded_half_up(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, amount_cents FROM transactions")).all())

    assert rows == {1: 435, 2: 10, 3: 1234, 4: 10000, 5: 0}


def test_migrated_budget_limits_are_rounded(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT category, limit_cents FROM budgets")).all())

    assert rows == {"Coffee": 2000, "Groceries": 50, "Other": 0}


def test_migration_preserves_row_count_and_metadata(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()
        row = conn.execute(
            text("SELECT description, date FROM transactions WHERE id = 1")
        ).one()

    assert count == 5
    assert row == ("Flat white", "2026-09-30")


def test_second_run_is_a_noop(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)

    second = migrations.run(engine)

    assert second == {"transactions_to_cents": False, "budgets_to_cents": False, "categories": 0}


def test_second_run_does_not_alter_values(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)
    migrations.run(engine)

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, amount_cents FROM transactions")).all())

    assert rows == {1: 435, 2: 10, 3: 1234, 4: 10000, 5: 0}


def test_fresh_database_is_seeded_and_versioned(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)

    report = migrations.run(engine)

    assert report["transactions_to_cents"] is False
    assert report["budgets_to_cents"] is False
    assert report["categories"] == 19

    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT value FROM app_meta WHERE key = 'schema_version'")
        ).scalar_one()
        kinds = dict(
            conn.execute(text("SELECT kind, COUNT(*) FROM categories GROUP BY kind")).all()
        )

    assert version == migrations.SCHEMA_VERSION
    assert kinds == {"expense": 13, "income": 6}


def test_seeding_never_duplicates_existing_categories(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    migrations.run(engine)

    report = migrations.run(engine)

    assert report["categories"] == 0
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM categories")).scalar_one()
    assert total == 19
