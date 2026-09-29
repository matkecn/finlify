"""Idempotent database migrations, applied automatically on every startup.

The only historical schema Finlify supports upgrading from is its own v1 layout,
which stored money as a SQLite ``REAL`` column. Both money columns are rebuilt
as integers here. Running :func:`run` against an already-migrated database is a
no-op, so it is safe to call on each boot.
"""

from __future__ import annotations

import logging
from datetime import date as dt_date

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from models import CATEGORY_PALETTE, AppMeta, Category

log = logging.getLogger("finlify.migrations")

SCHEMA_VERSION = "2"

DEFAULT_CATEGORIES = (
    ("Housing", "expense"), ("Groceries", "expense"), ("Dining", "expense"),
    ("Transport", "expense"), ("Utilities", "expense"), ("Healthcare", "expense"),
    ("Entertainment", "expense"), ("Shopping", "expense"), ("Subscriptions", "expense"),
    ("Travel", "expense"), ("Fitness", "expense"), ("Education", "expense"),
    ("Other", "expense"),
    ("Salary", "income"), ("Freelance", "income"), ("Investments", "income"),
    ("Gifts", "income"), ("Refunds", "income"), ("Other income", "income"),
)


def _columns(conn, table: str) -> set[str]:
    """Return the set of column names present on ``table``."""
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _has_table(conn, table: str) -> bool:
    """Return whether ``table`` exists in the connected database."""
    return table in inspect(conn).get_table_names()


def _migrate_transactions_to_cents(conn) -> bool:
    """Rebuild ``transactions``, replacing the float ``amount`` column with an
    integer ``amount_cents``.

    SQLite cannot alter a column's type in place, so the table is recreated and
    the rows copied across with each amount rounded half-up. Existing rows are
    otherwise preserved.

    Returns:
        ``True`` if a migration was performed, ``False`` if there was nothing to
        do or the table was absent.
    """
    if not _has_table(conn, "transactions"):
        return False

    cols = _columns(conn, "transactions")
    if "amount_cents" in cols:
        return False
    if "amount" not in cols:
        return False

    log.info("migrating transactions.amount (float) -> amount_cents (integer)")
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text(
        """
        CREATE TABLE transactions__new (
            id INTEGER PRIMARY KEY,
            amount_cents INTEGER NOT NULL,
            type TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date DATE NOT NULL
        )
        """
    ))
    conn.execute(text(
        """
        INSERT INTO transactions__new (id, amount_cents, type, category, description, date)
        SELECT id,
               CAST(ROUND(COALESCE(amount, 0) * 100.0) AS INTEGER),
               type, category, description, date
        FROM transactions
        """
    ))
    conn.execute(text("DROP TABLE transactions"))
    conn.execute(text("ALTER TABLE transactions__new RENAME TO transactions"))
    conn.execute(text("CREATE INDEX ix_transactions_id ON transactions (id)"))
    conn.execute(text("CREATE INDEX ix_transactions_category ON transactions (category)"))
    conn.execute(text("CREATE INDEX ix_transactions_date ON transactions (date)"))
    conn.execute(text("PRAGMA foreign_keys=ON"))
    return True


def _migrate_budgets_to_cents(conn) -> bool:
    """Rebuild ``budgets``, replacing the float ``limit_amount`` column with an
    integer ``limit_cents`` and rounding each limit half-up.

    Returns:
        ``True`` if a migration was performed, ``False`` if there was nothing to
        do or the table was absent.
    """
    if not _has_table(conn, "budgets"):
        return False
    cols = _columns(conn, "budgets")
    if "limit_cents" in cols:
        return False
    if "limit_amount" not in cols:
        return False

    log.info("migrating budgets.limit_amount (float) -> limit_cents (integer)")
    conn.execute(text(
        """
        CREATE TABLE budgets__new (
            id INTEGER PRIMARY KEY,
            category TEXT NOT NULL UNIQUE,
            limit_cents INTEGER NOT NULL DEFAULT 0
        )
        """
    ))
    conn.execute(text(
        """
        INSERT INTO budgets__new (id, category, limit_cents)
        SELECT id, category, CAST(ROUND(COALESCE(limit_amount, 0) * 100.0) AS INTEGER)
        FROM budgets
        """
    ))
    conn.execute(text("DROP TABLE budgets"))
    conn.execute(text("ALTER TABLE budgets__new RENAME TO budgets"))
    conn.execute(text("CREATE INDEX ix_budgets_id ON budgets (id)"))
    conn.execute(text("CREATE INDEX ix_budgets_category ON budgets (category)"))
    return True


def _seed_default_categories(db: Session) -> int:
    """Insert any missing starter categories, leaving existing ones untouched.

    Categories are user-owned, so this only ever adds names that are absent and
    never modifies or removes a row the user already has.

    Returns:
        The number of categories inserted.
    """
    existing = {row.name for row in db.query(Category).all()}
    added = 0
    for order, (name, kind) in enumerate(DEFAULT_CATEGORIES):
        if name in existing:
            continue
        db.add(Category(
            name=name,
            kind=kind,
            color=CATEGORY_PALETTE[order % len(CATEGORY_PALETTE)],
            sort_order=order,
        ))
        added += 1
    if added:
        db.commit()
        log.info("seeded %d default categories", added)
    return added


def run(engine) -> dict:
    """Bring the database up to :data:`SCHEMA_VERSION` and seed starter data.

    The legacy tables are reshaped before ``create_all`` runs, because
    ``create_all`` cannot change the type of an existing column.

    Args:
        engine: A SQLAlchemy engine bound to the SQLite database file.

    Returns:
        A report indicating which migrations ran and how many categories were
        seeded.
    """
    from database import Base
    import models

    report = {"transactions_to_cents": False, "budgets_to_cents": False, "categories": 0}

    with engine.begin() as conn:
        report["transactions_to_cents"] = _migrate_transactions_to_cents(conn)
        report["budgets_to_cents"] = _migrate_budgets_to_cents(conn)

    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        report["categories"] = _seed_default_categories(db)
        row = db.get(AppMeta, "schema_version")
        if row is None:
            db.add(AppMeta(key="schema_version", value=SCHEMA_VERSION))
        else:
            row.value = SCHEMA_VERSION
        db.commit()

    return report


if __name__ == "__main__":
    import logging as _logging

    from database import engine

    _logging.basicConfig(level=logging.INFO)
    print(run(engine))
