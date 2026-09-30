"""Idempotent database migrations, applied automatically on every startup.

Finlify has supported upgrading from its own v1 float-money layout, from the
pre-accounts layout, and now from the pre-login layout in which every row
belonged to a single implicit owner. Each step is a no-op once applied, so
:func:`run` is safe to call on every boot.
"""

from __future__ import annotations

import logging
import re
from datetime import date as dt_date

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from models import (
    ADMIN_USERNAME,
    ADMIN_USER_ID,
    CATEGORY_PALETTE,
    Account,
    AppMeta,
    Budget,
    Category,
    Transaction,
    User,
)

log = logging.getLogger("finlify.migrations")

SCHEMA_VERSION = "5"

DEFAULT_ACCOUNT = ("Main", "checking")

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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


def _unique_column_sets(conn, table: str) -> set[frozenset[str]]:
    """Return the column sets behind every unique index on ``table``."""
    if not SAFE_IDENTIFIER.match(table):
        raise ValueError(f"unsafe table name {table!r}")
    sets: set[frozenset[str]] = set()
    for row in conn.execute(text(f"PRAGMA index_list('{table}')")).fetchall():
        if len(row) < 3 or not row[2]:
            continue
        names = [
            info[2]
            for info in conn.execute(text(f"PRAGMA index_info('{row[1]}')")).fetchall()
        ]
        sets.add(frozenset(name for name in names if name))
    return sets


def _drop_stale_rebuild_tables(conn) -> list[str]:
    """Remove leftover ``*__new`` tables from a migration that was interrupted.

    A rebuild renames its staging table into place, so a ``__new`` table only
    exists if the process stopped midway. Dropping it is always safe because the
    original table is still intact at that point.

    Returns:
        The names of the tables that were dropped.
    """
    dropped = []
    rows = conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%\\_\\_new' ESCAPE '\\'"
    )).fetchall()
    for row in rows:
        name = row[0]
        if not SAFE_IDENTIFIER.match(name):
            continue
        conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        dropped.append(name)
    if dropped:
        log.info("dropped stale staging tables: %s", ", ".join(dropped))
    return dropped


def _add_user_column(conn, table: str) -> bool:
    """Add a ``user_id`` column to ``table`` if it is missing.

    Existing rows default to :data:`~models.ADMIN_USER_ID`, which is the id the
    admin user receives on a database that predates sign-in, so no row is ever
    orphaned by the upgrade.

    Returns:
        ``True`` if the column was added, ``False`` if it already existed or the
        table was absent.
    """
    if not _has_table(conn, table):
        return False
    if "user_id" in _columns(conn, table):
        return False
    log.info("adding %s.user_id column", table)
    conn.execute(text(
        f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT {ADMIN_USER_ID}"
    ))
    return True


def _make_user_scoped(conn, table: str, key: str, ddl: str, extra: tuple) -> bool:
    """Rebuild ``table`` so that ``key`` is unique per user instead of globally.

    SQLite cannot drop a unique constraint in place, so the table is recreated
    with a composite ``(user_id, key)`` constraint and every row is copied across
    unchanged. A table that is already scoped is left alone.

    Args:
        conn: An open connection to the database being migrated.
        table: The table to rebuild.
        key: The column whose uniqueness becomes per user.
        ddl: The ``CREATE TABLE`` statement for the replacement table.
        extra: Further columns to copy, as ``(column, fallback_expression)``
            pairs, where the fallback is used if the source lacks that column.

    Returns:
        ``True`` if the table was rebuilt, ``False`` if it was already scoped or
        could not be rebuilt.
    """
    if not _has_table(conn, table):
        return False
    cols = _columns(conn, table)
    if "user_id" not in cols or key not in cols:
        return False
    if frozenset({"user_id", key}) in _unique_column_sets(conn, table):
        return False

    source = {"id": "id", key: key, "user_id": "user_id"}
    ordered = ["id", key]
    for name, fallback in extra:
        if name in cols:
            ordered.append(name)
            source[name] = name
        elif fallback is not None:
            ordered.append(name)
            source[name] = fallback
    ordered.append("user_id")
    projection = ", ".join(source[name] for name in ordered)

    log.info("rebuilding %s so %s is unique per user", table, key)
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text(f"DROP TABLE IF EXISTS {table}__new"))
    conn.execute(text(ddl))
    conn.execute(text(
        f"INSERT INTO {table}__new ({', '.join(ordered)}) "
        f"SELECT {projection} FROM {table}"
    ))
    conn.execute(text(f"DROP TABLE {table}"))
    conn.execute(text(f"ALTER TABLE {table}__new RENAME TO {table}"))
    conn.execute(text(f"CREATE INDEX ix_{table}_id ON {table} (id)"))
    conn.execute(text(f"CREATE INDEX ix_{table}_{key} ON {table} ({key})"))
    conn.execute(text(f"CREATE INDEX ix_{table}_user_id ON {table} (user_id)"))
    conn.execute(text("PRAGMA foreign_keys=ON"))
    return True


BUDGETS_DDL = """
        CREATE TABLE budgets__new (
            id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            limit_cents INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT uq_budgets_user_category UNIQUE (user_id, category)
        )
        """

CATEGORIES_DDL = """
        CREATE TABLE categories__new (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'expense',
            color TEXT NOT NULL DEFAULT '#5eead4',
            is_archived INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT uq_categories_user_name UNIQUE (user_id, name)
        )
        """

ACCOUNTS_DDL = """
        CREATE TABLE accounts__new (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'checking',
            color TEXT NOT NULL DEFAULT '#5eead4',
            opening_balance_cents INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT uq_accounts_user_name UNIQUE (user_id, name)
        )
        """


def _add_user_date_index(conn) -> bool:
    """Create the ``(user_id, date)`` index the ledger query relies on.

    ``create_all`` skips tables that already exist, so an upgrade would never
    gain the index declared on the model. This adds it explicitly. Older
    databases also lack a ``user_id`` column, in which case the later user-scope
    migration and the next boot handle it.

    Returns:
        ``True`` if the index was created, ``False`` if it already existed or the
        table or column was absent.
    """
    if not _has_table(conn, "transactions"):
        return False
    if "user_id" not in _columns(conn, "transactions"):
        return False
    rows = conn.execute(text("PRAGMA index_list('transactions')")).fetchall()
    if "ix_transactions_user_date" in {row[1] for row in rows}:
        return False
    log.info("creating ix_transactions_user_date index")
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_transactions_user_date "
        "ON transactions (user_id, date)"
    ))
    return True


def _scope_budgets(conn) -> bool:
    """Make ``budgets.category`` unique per user rather than globally."""
    return _make_user_scoped(
        conn, "budgets", "category", BUDGETS_DDL, (("limit_cents", "0"),)
    )


def _scope_categories(conn) -> bool:
    """Make ``categories.name`` unique per user rather than globally."""
    return _make_user_scoped(
        conn,
        "categories",
        "name",
        CATEGORIES_DDL,
        (
            ("kind", "'expense'"),
            ("color", f"'{CATEGORY_PALETTE[0]}'"),
            ("is_archived", "0"),
            ("sort_order", "0"),
        ),
    )


def _scope_accounts(conn) -> bool:
    """Make ``accounts.name`` unique per user rather than globally."""
    return _make_user_scoped(
        conn,
        "accounts",
        "name",
        ACCOUNTS_DDL,
        (
            ("kind", "'checking'"),
            ("color", f"'{CATEGORY_PALETTE[0]}'"),
            ("opening_balance_cents", "0"),
            ("is_archived", "0"),
            ("sort_order", "0"),
        ),
    )


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
    conn.execute(text("DROP TABLE IF EXISTS transactions__new"))
    conn.execute(text(
        """
        CREATE TABLE transactions__new (
            id INTEGER PRIMARY KEY,
            amount_cents INTEGER NOT NULL,
            type TEXT NOT NULL,
            category TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT 'Main',
            description TEXT,
            date DATE NOT NULL
        )
        """
    ))
    conn.execute(text(
        """
        INSERT INTO transactions__new (id, amount_cents, type, category, account, description, date)
        SELECT id,
               CAST(ROUND(COALESCE(amount, 0) * 100.0) AS INTEGER),
               type, category, 'Main', description, date
        FROM transactions
        """
    ))
    conn.execute(text("DROP TABLE transactions"))
    conn.execute(text("ALTER TABLE transactions__new RENAME TO transactions"))
    conn.execute(text("CREATE INDEX ix_transactions_id ON transactions (id)"))
    conn.execute(text("CREATE INDEX ix_transactions_category ON transactions (category)"))
    conn.execute(text("CREATE INDEX ix_transactions_account ON transactions (account)"))
    conn.execute(text("CREATE INDEX ix_transactions_date ON transactions (date)"))
    conn.execute(text("PRAGMA foreign_keys=ON"))
    return True


def _add_account_column(conn) -> bool:
    """Add the ``transactions.account`` column to a pre-accounts database.

    SQLite supports ``ADD COLUMN`` with a constant default, so the column is
    added in place and every existing row is filed under the ``Main`` account.
    Databases created after accounts shipped already have the column and skip
    this step.

    Returns:
        ``True`` if the column was added, ``False`` if it already existed or the
        table was absent.
    """
    if not _has_table(conn, "transactions"):
        return False
    if "account" in _columns(conn, "transactions"):
        return False

    log.info("adding transactions.account column")
    conn.execute(text(
        "ALTER TABLE transactions ADD COLUMN account TEXT NOT NULL DEFAULT 'Main'"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_transactions_account ON transactions (account)"
    ))
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
    conn.execute(text("DROP TABLE IF EXISTS budgets__new"))
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


def _seed_default_categories(db: Session, user_id: int) -> int:
    """Insert any missing starter categories for one user.

    Categories are user-owned, so this only ever adds names that user is missing
    and never modifies or removes a row they already have.

    Args:
        db: An open session.
        user_id: The user to seed categories for.

    Returns:
        The number of categories inserted.
    """
    existing = {row.name for row in db.query(Category).filter(
        Category.user_id == user_id).all()}
    added = 0
    for order, (name, kind) in enumerate(DEFAULT_CATEGORIES):
        if name in existing:
            continue
        db.add(Category(
            name=name,
            kind=kind,
            color=CATEGORY_PALETTE[order % len(CATEGORY_PALETTE)],
            sort_order=order,
            user_id=user_id,
        ))
        added += 1
    if added:
        db.commit()
        log.info("seeded %d default categories for user %s", added, user_id)
    return added


def _seed_default_account(db: Session, user_id: int) -> int:
    """Insert a starter ``Main`` account for one user when they have none.

    Accounts are user-owned, but a ledger must always have somewhere to post,
    so a single default is created only when that user has no accounts at all.
    Any existing account, including one renamed by the user, is left untouched.

    Args:
        db: An open session.
        user_id: The user to seed an account for.

    Returns:
        The number of accounts inserted, either ``0`` or ``1``.
    """
    existing = db.query(Account).filter(Account.user_id == user_id).first()
    if existing is not None:
        return 0
    name, kind = DEFAULT_ACCOUNT
    db.add(Account(
        name=name,
        kind=kind,
        color=CATEGORY_PALETTE[0],
        opening_balance_cents=0,
        sort_order=0,
        user_id=user_id,
    ))
    db.commit()
    log.info("seeded default account %r for user %s", name, user_id)
    return 1


def _ensure_admin_user(db: Session) -> tuple[User, bool]:
    """Return the administrator, creating a claimable placeholder if needed.

    The administrator is the account that can never be deleted, so it always
    exists. A freshly created placeholder has no password, which is what makes
    the application ask the owner to claim their ledger on first launch.

    Args:
        db: An open session.

    Returns:
        The admin user and whether it was created by this call.
    """
    admin = db.query(User).filter(User.is_admin.is_(True)).first()
    if admin is not None:
        return admin, False
    first = db.query(User).order_by(User.id).first()
    if first is not None:
        first.is_admin = True
        db.commit()
        return first, False
    admin = User(
        username=ADMIN_USERNAME,
        display_name="Administrator",
        password_hash=None,
        is_admin=True,
    )
    db.add(admin)
    db.commit()
    log.info("created placeholder admin %r awaiting first-launch setup", ADMIN_USERNAME)
    return admin, True


def _claim_rows(db: Session, user_id: int) -> dict[str, int]:
    """Point every existing row at one user.

    This runs only when upgrading a database that predates sign-in, where all
    data implicitly belonged to the person using the machine. Their ledger
    becomes the admin's so nothing is lost.

    Args:
        db: An open session.
        user_id: The user that should own every existing row.

    Returns:
        A mapping of table name to the number of rows reassigned.
    """
    moved: dict[str, int] = {}
    for model in (Transaction, Category, Budget, Account):
        result = db.query(model).filter(model.user_id != user_id).update(
            {model.user_id: user_id}, synchronize_session=False
        )
        moved[model.__tablename__] = result
    db.commit()
    if any(moved.values()):
        log.info("claimed existing rows for user %s: %s", user_id, moved)
    return moved


def _owned_row_counts(db: Session, user_id: int) -> dict[str, int]:
    """Return how many rows of each owned table belong to one user.

    Args:
        db: An open session.
        user_id: The user whose rows should be counted.

    Returns:
        A mapping of table name to that user's row count.
    """
    return {
        model.__tablename__: db.query(model).filter(model.user_id == user_id).count()
        for model in (Transaction, Category, Budget, Account)
    }


def run(engine) -> dict:
    """Bring the database up to :data:`SCHEMA_VERSION` and seed starter data.

    Legacy reshaping happens before ``create_all`` runs, because ``create_all``
    cannot change an existing column's type or drop a constraint.

    Args:
        engine: A SQLAlchemy engine bound to the SQLite database file.

    Returns:
        A report indicating which migrations ran, how many rows the
        administrator ended up owning, and how many starter rows each user
        received.
    """
    from database import Base
    import models

    report = {
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

    with engine.begin() as conn:
        report["transactions_to_cents"] = _migrate_transactions_to_cents(conn)
        report["budgets_to_cents"] = _migrate_budgets_to_cents(conn)
        report["account_column"] = _add_account_column(conn)
        report["dropped_staging"] = _drop_stale_rebuild_tables(conn)
        had_users = _has_table(conn, "users")
        for table in ("transactions", "categories", "budgets", "accounts"):
            report["user_columns"] += int(_add_user_column(conn, table))
        report["scoped_budgets"] = _scope_budgets(conn)
        report["scoped_categories"] = _scope_categories(conn)
        report["scoped_accounts"] = _scope_accounts(conn)
        report["user_date_index"] = _add_user_date_index(conn)

    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        admin, created = _ensure_admin_user(db)
        first_run = not had_users or created
        if first_run:
            _claim_rows(db, admin.id)
        for user in db.query(User).order_by(User.id).all():
            report["categories"] += _seed_default_categories(db, user.id)
            report["accounts"] += _seed_default_account(db, user.id)
        if first_run:
            report["claimed"] = _owned_row_counts(db, admin.id)
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
