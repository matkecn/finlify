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


def _v2_engine(path):
    """Create a database using the cents schema that predates accounts."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE transactions ("
                "id INTEGER PRIMARY KEY, amount_cents INTEGER NOT NULL, "
                "type TEXT NOT NULL, category TEXT NOT NULL, description TEXT, "
                "date DATE NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE budgets ("
                "id INTEGER PRIMARY KEY, category TEXT NOT NULL UNIQUE, "
                "limit_cents INTEGER NOT NULL DEFAULT 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO transactions (id, amount_cents, type, category, description, date) VALUES "
                "(1, 435, 'expense', 'Coffee', 'Flat white', '2026-09-30'),"
                "(2, 1234, 'income', 'Salary', NULL, '2026-09-01')"
            )
        )
    return engine


def _column_names(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _index_names(engine, table):
    return {index["name"] for index in inspect(engine).get_indexes(table)}


def test_migrates_float_money_columns_to_integer_cents(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")

    report = migrations.run(engine)

    assert report["transactions_to_cents"] is True
    assert report["budgets_to_cents"] is True
    assert report["categories"] == 19

    assert "amount_cents" in _column_names(engine, "transactions")
    assert "amount" not in _column_names(engine, "transactions")
    assert "account" in _column_names(engine, "transactions")
    assert "limit_cents" in _column_names(engine, "budgets")
    assert "limit_amount" not in _column_names(engine, "budgets")


def test_adds_account_column_to_cents_schema(tmp_path):
    engine = _v2_engine(tmp_path / "v2.db")

    report = migrations.run(engine)

    assert report["account_column"] is True
    assert report["accounts"] == 1

    with engine.connect() as conn:
        accounts = set(
            conn.execute(text("SELECT account FROM transactions")).scalars().all()
        )
        seeded = conn.execute(text("SELECT name FROM accounts")).scalars().all()

    assert accounts == {"Main"}
    assert list(seeded) == ["Main"]


def test_account_column_migration_is_idempotent(tmp_path):
    engine = _v2_engine(tmp_path / "v2.db")
    migrations.run(engine)

    second = migrations.run(engine)

    assert second["account_column"] is False


def test_migration_creates_the_user_date_index(tmp_path):
    engine = _v2_engine(tmp_path / "v2.db")

    report = migrations.run(engine)

    assert report["user_date_index"] is True
    assert "ix_transactions_user_date" in _index_names(engine, "transactions")


def test_user_date_index_migration_is_idempotent(tmp_path):
    engine = _v2_engine(tmp_path / "v2.db")
    migrations.run(engine)

    second = migrations.run(engine)

    assert second["user_date_index"] is False


def test_fresh_database_has_the_user_date_index(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)

    report = migrations.run(engine)

    assert report["user_date_index"] is False
    assert "ix_transactions_user_date" in _index_names(engine, "transactions")


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

    assert second == {
        "transactions_to_cents": False,
        "budgets_to_cents": False,
        "account_column": False,
        "dropped_staging": [],
        "user_columns": 0,
        "scoped_budgets": False,
        "scoped_categories": False,
        "scoped_accounts": False,
        "user_date_index": False,
        "claimed": {},
        "categories": 0,
        "accounts": 0,
    }


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
    assert report["account_column"] is False
    assert report["categories"] == 19
    assert report["accounts"] == 1

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


def test_migration_creates_a_claimable_administrator(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")

    report = migrations.run(engine)

    assert report["claimed"] == {
        "transactions": 5,
        "categories": 19,
        "budgets": 3,
        "accounts": 1,
    }
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, username, password_hash, is_admin FROM users")
        ).one()
    assert row[0] == migrations.ADMIN_USER_ID
    assert row[1] == migrations.ADMIN_USERNAME
    assert row[2] is None
    assert row[3] == 1


def test_migration_claims_every_legacy_row_for_the_owner(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")

    migrations.run(engine)

    with engine.connect() as conn:
        owners = {
            table: {
                r[0]
                for r in conn.execute(
                    text(f"SELECT DISTINCT user_id FROM {table}")
                ).all()
            }
            for table in ("transactions", "budgets", "categories", "accounts")
        }
        counts = dict(
            conn.execute(
                text(
                    "SELECT 'transactions', COUNT(*) FROM transactions "
                    "UNION ALL SELECT 'budgets', COUNT(*) FROM budgets "
                    "UNION ALL SELECT 'categories', COUNT(*) FROM categories "
                    "UNION ALL SELECT 'accounts', COUNT(*) FROM accounts"
                )
            ).all()
        )

    assert owners == {
        "transactions": {1},
        "budgets": {1},
        "categories": {1},
        "accounts": {1},
    }
    assert counts == {
        "transactions": 5,
        "budgets": 3,
        "categories": 19,
        "accounts": 1,
    }


def test_migration_adds_a_user_column_to_every_owned_table(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")

    migrations.run(engine)

    for table in ("transactions", "categories", "budgets", "accounts"):
        assert "user_id" in _column_names(engine, table)


def test_migration_leaves_a_claimed_owner_untouched(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET password_hash = 'already-set', username = 'owner'")
        )

    report = migrations.run(engine)

    assert report["claimed"] == {}
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT username, password_hash FROM users WHERE id = 1")
        ).one()
    assert row == ("owner", "already-set")


def _add_second_user(conn):
    conn.execute(
        text(
            "INSERT INTO users (id, username, display_name, currency, theme, "
            "is_admin, created_at) VALUES "
            "(2, 'sam', 'Sam', 'EUR', 'dark', 0, '2026-01-01 00:00:00')"
        )
    )


def test_migration_rebuilds_uniques_per_user(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)

    with engine.begin() as conn:
        _add_second_user(conn)
        conn.execute(
            text(
                "INSERT INTO categories (name, kind, color, is_archived, sort_order, user_id) "
                "VALUES ('Housing', 'expense', '#5eead4', 0, 0, 2)"
            )
        )

    with engine.connect() as conn:
        owners = dict(
            conn.execute(
                text(
                    "SELECT user_id, COUNT(*) FROM categories WHERE name = 'Housing' "
                    "GROUP BY user_id"
                )
            ).all()
        )

    assert owners == {1: 1, 2: 1}


def test_migration_drops_stale_staging_tables(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE budgets__new (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO budgets__new (id) VALUES (99)"))

    report = migrations.run(engine)

    assert "budgets__new" in report["dropped_staging"]
    assert "budgets__new" not in set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM budgets")).scalar_one() == 3


def test_user_ledgers_do_not_collide_on_shared_names(tmp_path):
    engine = _legacy_engine(tmp_path / "legacy.db")
    migrations.run(engine)
    with engine.begin() as conn:
        _add_second_user(conn)
        conn.execute(
            text(
                "INSERT INTO budgets (category, limit_cents, user_id) "
                "VALUES ('Coffee', 999, 2)"
            )
        )

    with engine.connect() as conn:
        rows = dict(
            conn.execute(text("SELECT category, limit_cents FROM budgets")).all()
        )
    assert rows["Coffee"] == 999
