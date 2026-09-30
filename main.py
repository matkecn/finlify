"""Finlify HTTP API and page routes.

A local-first personal finance service backed by SQLite. All monetary input
arrives as a decimal amount in major units and is stored as integer cents; all
monetary output is a float for display alongside the exact integer cents, so a
client can verify totals without reintroducing floating point drift.
"""

from __future__ import annotations

import calendar
from datetime import date as dt_date
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

import auth
import migrations
import preferences
from database import Base, SessionLocal, engine
from models import (
    ACCOUNT_KINDS,
    CATEGORY_KINDS,
    CATEGORY_PALETTE,
    TRANSACTION_TYPES,
    Account,
    AuthSession,
    Budget,
    Category,
    Transaction,
    User,
    normalize_color,
    normalize_label,
)
from money import MoneyError, from_cents, parse_signed_cents, to_cents, to_signed_cents

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "finlify.db"
EXPORT_VERSION = 1

SESSION_COOKIE = auth.COOKIE_NAME

app = FastAPI(
    title="Finlify",
    description="Local-first personal finance. Data never leaves this machine.",
    version="2.0.0",
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

migrations.run(engine)


def get_db():
    """Yield a database session that is closed when the request finishes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def signed_in_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Return the user behind the session cookie, or ``None`` if signed out."""
    return auth.resolve_session(db, request.cookies.get(SESSION_COOKIE))


def current_user(user: User | None = Depends(signed_in_user)) -> User:
    """Return the signed-in user, rejecting the request if there is none.

    Raises:
        HTTPException: With status 401 when the session cookie is missing,
            unknown, or expired.
    """
    if user is None:
        raise HTTPException(401, "Sign in to continue")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    """Return the signed-in user, rejecting the request if not an administrator.

    Raises:
        HTTPException: With status 401 when signed out, or 403 when the account
            is not the administrator.
    """
    if not user.is_admin:
        raise HTTPException(403, "Only the administrator can do that")
    return user


def set_session_cookie(response: Response, token: str) -> None:
    """Attach a freshly minted session cookie to a response.

    Routes call this and then return a body, because returning the injected
    response object itself would bypass the declared status code and body.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )


def auth_payload(user: User) -> dict:
    """Return the body describing a signed-in user and their preferences."""
    return {
        "user": user.to_dict(),
        "currencies": preferences.currency_options(),
        "themes": list(preferences.THEMES),
    }


def needs_setup(db: Session) -> bool:
    """Return whether the owner still has to claim this ledger with a password."""
    admin = db.query(User).filter(User.is_admin.is_(True)).first()
    return admin is None or not admin.has_password


def seed_new_user(db: Session, user: User) -> None:
    """Give a brand new user their own starter categories and account."""
    for order, (name, kind) in enumerate(migrations.DEFAULT_CATEGORIES):
        if db.query(Category).filter(
            Category.user_id == user.id, Category.name == name
        ).first():
            continue
        db.add(Category(
            name=name,
            kind=kind,
            color=colour_for(order),
            sort_order=order,
            user_id=user.id,
        ))
    if not db.query(Account).filter(Account.user_id == user.id).first():
        name, kind = migrations.DEFAULT_ACCOUNT
        db.add(Account(
            name=name,
            kind=kind,
            color=colour_for(0),
            opening_balance_cents=0,
            sort_order=0,
            user_id=user.id,
        ))
    db.commit()


def unique_username(db: Session, username: str, exclude_id: int | None = None) -> str:
    """Return a normalised username that no other account is already using."""
    name = " ".join(str(username or "").split()).lower()[:40]
    if not name:
        raise HTTPException(422, "Username cannot be empty")
    taken = db.query(User).filter(func.lower(User.username) == name)
    if exclude_id is not None:
        taken = taken.filter(User.id != exclude_id)
    if taken.first() is not None:
        raise HTTPException(409, f"Username '{name}' is already taken")
    return name


def admin_user(db: Session) -> User:
    """Return the administrator, raising 404 if the ledger somehow has none."""
    admin = db.query(User).filter(User.is_admin.is_(True)).first()
    if admin is None:
        raise HTTPException(404, "No administrator exists")
    return admin


def owned(model, db: Session, user_id: int, row_id: int, label: str):
    """Return one of a user's rows by id, raising 404 when it is not theirs.

    Scoping the lookup itself is what stops one person reading, editing, or
    deleting another person's rows simply by guessing an id.

    Args:
        model: The ORM class to look up.
        db: An open database session.
        user_id: The user that must own the row.
        row_id: The primary key to fetch.
        label: Human readable noun used in the 404 message.

    Returns:
        The matching ORM row.

    Raises:
        HTTPException: With status 404 if the user has no such row.
    """
    row = db.query(model).filter(model.id == row_id, model.user_id == user_id).first()
    if row is None:
        raise HTTPException(404, f"{label} not found")
    return row


def month_start(d: dt_date) -> dt_date:
    """Return the first day of the month containing ``d``."""
    return d.replace(day=1)


def add_months(d: dt_date, months: int) -> dt_date:
    """Return the first day of the month ``months`` away from ``d``."""
    index = d.month - 1 + months
    return dt_date(d.year + index // 12, index % 12 + 1, 1)


def month_label(d: dt_date, short: bool = True) -> str:
    """Return the month name for ``d``, abbreviated when ``short`` is true."""
    return calendar.month_abbr[d.month] if short else calendar.month_name[d.month]


def pct_change(current: int, previous: int) -> float | None:
    """Return the percentage change between two cent amounts.

    Returns ``None`` when the previous value is zero, because the change is
    undefined rather than infinite, and ``0.0`` when both are zero.
    """
    if previous == 0:
        return None if current else 0.0
    return round((current - previous) / abs(previous) * 100, 2)


def colour_for(index: int) -> str:
    """Return a stable palette colour for the given position."""
    return CATEGORY_PALETTE[index % len(CATEGORY_PALETTE)]


def known_category_names(db: Session, user_id: int) -> set[str]:
    """Return the names of every category one user currently defines."""
    return {
        row.name
        for row in db.query(Category).filter(Category.user_id == user_id).all()
    }


def assert_category_known(db: Session, user_id: int, name: str) -> str:
    """Return ``name`` if it is a category this user defines.

    Raises:
        HTTPException: With status 422 if no such category exists, telling the
            caller to create it first.
    """
    if name not in known_category_names(db, user_id):
        raise HTTPException(422, f"Unknown category '{name}'. Create it first.")
    return name


def known_account_names(db: Session, user_id: int) -> set[str]:
    """Return the names of every account one user currently defines."""
    return {
        row.name
        for row in db.query(Account).filter(Account.user_id == user_id).all()
    }


def assert_account_known(db: Session, user_id: int, name: str) -> str:
    """Return ``name`` if it is an account this user defines.

    Raises:
        HTTPException: With status 422 if no such account exists, telling the
            caller to create it first.
    """
    if name not in known_account_names(db, user_id):
        raise HTTPException(422, f"Unknown account '{name}'. Create it first.")
    return name


def clean_label(value: str) -> str:
    """Collapse whitespace in a user-supplied label and cap its length."""
    return " ".join(str(value).split())[:40]


def account_rows(db: Session, user_id: int) -> list[dict]:
    """Return every account with its running balance and activity totals.

    An account's balance is its opening balance plus its signed transaction
    sum, where income adds and expense subtracts. Archived accounts are included
    so that money moved out of a closed account is never lost from the net
    worth total; callers that need an active-only list can filter on
    ``is_archived``.

    Returns:
        Account dictionaries ordered by sort order then name, each augmented
        with ``income_cents``, ``expenses_cents``, ``net_cents``, ``balance``,
        and ``balance_cents``.
    """
    income: dict[str, int] = {}
    expenses: dict[str, int] = {}
    counts: dict[str, int] = {}
    for t in db.query(Transaction).filter(Transaction.user_id == user_id).all():
        if not t.is_classified:
            continue
        counts[t.account] = counts.get(t.account, 0) + 1
        if t.type == "income":
            income[t.account] = income.get(t.account, 0) + t.amount_cents
        else:
            expenses[t.account] = expenses.get(t.account, 0) + t.amount_cents

    rows = []
    for account in db.query(Account).filter(
        Account.user_id == user_id
    ).order_by(Account.sort_order, Account.name).all():
        earned = income.get(account.name, 0)
        spent = expenses.get(account.name, 0)
        balance = account.opening_balance_cents + earned - spent
        entry = account.to_dict()
        entry.update({
            "income": from_cents(earned),
            "income_cents": earned,
            "expenses": from_cents(spent),
            "expenses_cents": spent,
            "net": from_cents(earned - spent),
            "net_cents": earned - spent,
            "balance": from_cents(balance),
            "balance_cents": balance,
            "transaction_count": counts.get(account.name, 0),
        })
        rows.append(entry)
    return rows


def account_entry(db: Session, user_id: int, account_id: int) -> dict:
    """Return a single account row with its balance and activity totals."""
    return next(row for row in account_rows(db, user_id) if row["id"] == account_id)


def account_name_taken(
    db: Session, user_id: int, name: str, exclude_id: int | None = None
) -> bool:
    """Whether this user already has an account called ``name``, ignoring case."""
    query = db.query(Account).filter(
        Account.user_id == user_id,
        func.lower(Account.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.filter(Account.id != exclude_id)
    return query.first() is not None


class TransactionIn(BaseModel):
    """Payload for creating or updating a transaction.

    ``amount`` is expressed in major units, for example ``19.99``. It is
    validated here and stored as integer cents.
    """

    amount: Decimal = Field(description="Major units, e.g. 19.99")
    type: str
    category: str = Field(min_length=1, max_length=40)
    account: str = Field(default="Main", min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=140)
    date: dt_date

    @field_validator("amount")
    @classmethod
    def _valid_amount(cls, v: Decimal) -> Decimal:
        """Reject amounts that cannot be represented as positive cents."""
        to_cents(v)
        return v

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        """Require the type to be one of :data:`TRANSACTION_TYPES`."""
        value = v.strip().lower()
        if value not in TRANSACTION_TYPES:
            raise ValueError(f"type must be one of {TRANSACTION_TYPES}")
        return value

    @field_validator("category")
    @classmethod
    def _clean_category(cls, v: str) -> str:
        """Normalise the category name, preserving deliberate capitalisation."""
        cleaned = " ".join(v.split())[:40]
        if not cleaned:
            raise ValueError("Category cannot be empty")
        if cleaned == cleaned.lower():
            return cleaned[:1].upper() + cleaned[1:]
        return cleaned

    @field_validator("account")
    @classmethod
    def _clean_account(cls, v: str) -> str:
        """Normalise the account name, preserving deliberate capitalisation."""
        cleaned = " ".join(v.split())[:40]
        if not cleaned:
            raise ValueError("Account cannot be empty")
        if cleaned == cleaned.lower():
            return cleaned[:1].upper() + cleaned[1:]
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_description(cls, v: str | None) -> str | None:
        """Collapse whitespace, treating an empty string as absent."""
        if v is None:
            return None
        cleaned = " ".join(v.split())[:140]
        return cleaned or None


class CategoryIn(BaseModel):
    """Payload for creating a category."""

    name: str = Field(min_length=1, max_length=40)
    kind: str = "expense"
    color: str | None = Field(default=None, max_length=7)
    is_archived: bool = False

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        """Require the kind to be one of :data:`CATEGORY_KINDS`."""
        kind = v.strip().lower()
        if kind not in CATEGORY_KINDS:
            raise ValueError(f"kind must be one of {CATEGORY_KINDS}")
        return kind

    @field_validator("color")
    @classmethod
    def _valid_color(cls, v: str | None) -> str | None:
        """Require a hex colour, or allow ``None`` for a default."""
        return normalize_color(v)


class CategoryPatch(BaseModel):
    """Payload for a partial update to a category; omitted fields are unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=40)
    kind: str | None = None
    color: str | None = Field(default=None, max_length=7)
    is_archived: bool | None = None

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str | None) -> str | None:
        """Require the kind to be one of :data:`CATEGORY_KINDS` when present."""
        if v is None:
            return None
        kind = v.strip().lower()
        if kind not in CATEGORY_KINDS:
            raise ValueError(f"kind must be one of {CATEGORY_KINDS}")
        return kind

    @field_validator("color")
    @classmethod
    def _valid_color(cls, v: str | None) -> str | None:
        """Require a hex colour, or allow ``None`` to leave it alone."""
        return normalize_color(v)


class AccountIn(BaseModel):
    """Payload for creating an account.

    ``opening_balance`` is expressed in major units and may be zero or negative,
    because a credit account can start in the red.
    """

    name: str = Field(min_length=1, max_length=40)
    kind: str = "checking"
    color: str | None = Field(default=None, max_length=7)
    opening_balance: Decimal = Decimal("0")
    is_archived: bool = False

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        """Require the kind to be one of :data:`ACCOUNT_KINDS`."""
        kind = v.strip().lower()
        if kind not in ACCOUNT_KINDS:
            raise ValueError(f"kind must be one of {ACCOUNT_KINDS}")
        return kind

    @field_validator("color")
    @classmethod
    def _valid_color(cls, v: str | None) -> str | None:
        """Require a hex colour, or allow ``None`` for a default."""
        return normalize_color(v)

    @field_validator("opening_balance")
    @classmethod
    def _valid_opening_balance(cls, v: Decimal) -> Decimal:
        """Reject opening balances that cannot be represented as signed cents."""
        to_signed_cents(v)
        return v


class AccountPatch(BaseModel):
    """Payload for a partial update to an account; omitted fields are unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=40)
    kind: str | None = None
    color: str | None = Field(default=None, max_length=7)
    opening_balance: Decimal | None = None
    is_archived: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str | None) -> str | None:
        """Require the kind to be one of :data:`ACCOUNT_KINDS` when present."""
        if v is None:
            return None
        kind = v.strip().lower()
        if kind not in ACCOUNT_KINDS:
            raise ValueError(f"kind must be one of {ACCOUNT_KINDS}")
        return kind

    @field_validator("color")
    @classmethod
    def _valid_color(cls, v: str | None) -> str | None:
        """Require a hex colour, or allow ``None`` to leave it alone."""
        return normalize_color(v)

    @field_validator("opening_balance")
    @classmethod
    def _valid_opening_balance(cls, v: Decimal | None) -> Decimal | None:
        """Reject opening balances that cannot be represented as signed cents."""
        if v is None:
            return None
        to_signed_cents(v)
        return v


class BudgetIn(BaseModel):
    """Payload for setting a monthly budget limit in major units.

    A limit of zero is allowed and clears the budget's spend tracking while
    keeping the category visible in the dashboard.
    """

    limit: Decimal = Field(ge=0)

    @field_validator("limit")
    @classmethod
    def _valid_limit(cls, v: Decimal) -> Decimal:
        """Reject a non zero limit that cannot be represented as cents."""
        if v > 0:
            to_cents(v)
        return v


def load_transactions(
    db: Session,
    user_id: int,
    start: dt_date | None = None,
    end: dt_date | None = None,
):
    """Return one user's transactions in ascending date order.

    Args:
        db: An open database session.
        user_id: The user whose ledger to read.
        start: Inclusive lower bound on the transaction date.
        end: Exclusive upper bound on the transaction date.
    """
    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    if start is not None:
        query = query.filter(Transaction.date >= start)
    if end is not None:
        query = query.filter(Transaction.date < end)
    return query.order_by(Transaction.date.asc(), Transaction.id.asc()).all()


def totals_cents(transactions) -> tuple[int, int]:
    """Return total income and expense cents, ignoring unclassified rows."""
    income = sum(t.amount_cents for t in transactions if t.type == "income")
    expenses = sum(t.amount_cents for t in transactions if t.type == "expense")
    return income, expenses


def build_timeseries(db: Session, user_id: int, months: int) -> list[dict]:
    """Return one bucket per month covering the trailing ``months`` window.

    Months with no activity are included as zero buckets so the chart keeps a
    continuous x axis.
    """
    today = dt_date.today()
    last = month_start(today)
    first = add_months(last, -(months - 1))

    buckets: dict[str, dict] = {}
    for offset in range(months):
        cursor = add_months(first, offset)
        buckets[cursor.strftime("%Y-%m")] = {
            "month": cursor.strftime("%Y-%m"),
            "label": month_label(cursor),
            "year": cursor.year,
            "income_cents": 0,
            "expenses_cents": 0,
            "count": 0,
        }

    for t in load_transactions(db, user_id, start=first):
        bucket = buckets.get(t.date.strftime("%Y-%m"))
        if bucket is None:
            continue
        if t.type == "income":
            bucket["income_cents"] += t.amount_cents
        elif t.type == "expense":
            bucket["expenses_cents"] += t.amount_cents
        else:
            continue
        bucket["count"] += 1

    out = []
    for bucket in buckets.values():
        income, expenses = bucket["income_cents"], bucket["expenses_cents"]
        out.append({
            "month": bucket["month"],
            "label": bucket["label"],
            "year": bucket["year"],
            "income": from_cents(income),
            "expenses": from_cents(expenses),
            "net": from_cents(income - expenses),
            "count": bucket["count"],
        })
    return out


def build_breakdown(db: Session, user_id: int, months: int, kind: str) -> list[dict]:
    """Return per category totals for the given type over a trailing window.

    Results are ordered by total descending, with each entry carrying its
    percentage share of the overall total.
    """
    today = dt_date.today()
    start = add_months(month_start(today), -(months - 1))

    totals: dict[str, dict] = {}
    for t in load_transactions(db, user_id, start=start):
        if t.type != kind:
            continue
        entry = totals.setdefault(t.category, {"total_cents": 0, "count": 0})
        entry["total_cents"] += t.amount_cents
        entry["count"] += 1

    grand = sum(e["total_cents"] for e in totals.values())
    palette = {
        c.name: c.color
        for c in db.query(Category).filter(Category.user_id == user_id).all()
    }
    ordered = sorted(totals.items(), key=lambda kv: kv[1]["total_cents"], reverse=True)

    result = []
    for index, (name, entry) in enumerate(ordered):
        result.append({
            "category": name,
            "total": from_cents(entry["total_cents"]),
            "total_cents": entry["total_cents"],
            "count": entry["count"],
            "pct": round(entry["total_cents"] / grand * 100, 2) if grand else 0.0,
            "color": palette.get(name) or colour_for(index),
        })
    return result


def _breakdown_for_month(db: Session, user_id: int, month: dt_date) -> list[dict]:
    """Return expense totals for one calendar month only.

    Used to compare consecutive months, which the range based
    :func:`build_breakdown` cannot do.
    """
    start = month_start(month)
    end = add_months(start, 1)
    totals: dict[str, int] = {}
    for t in load_transactions(db, user_id, start=start, end=end):
        if t.type == "expense":
            totals[t.category] = totals.get(t.category, 0) + t.amount_cents
    return [
        {"category": name, "total_cents": value}
        for name, value in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]


def build_budget_status(
    db: Session, user_id: int, start: dt_date, end: dt_date
) -> list[dict]:
    """Return each budget with its spend for the given period and a state.

    The state is ``ok`` below 80 percent used, ``warn`` from 80 to 100 percent,
    and ``over`` above 100 percent.
    """
    budgets = (
        db.query(Budget)
        .filter(Budget.user_id == user_id)
        .order_by(Budget.category)
        .all()
    )
    if not budgets:
        return []

    spent: dict[str, int] = {}
    for t in load_transactions(db, user_id, start=start, end=end):
        if t.type == "expense":
            spent[t.category] = spent.get(t.category, 0) + t.amount_cents

    palette = {
        c.name: c.color
        for c in db.query(Category).filter(Category.user_id == user_id).all()
    }
    status = []
    for index, budget in enumerate(budgets):
        used = spent.get(budget.category, 0)
        limit = budget.limit_cents
        ratio = (used / limit * 100) if limit else 0.0
        status.append({
            "category": budget.category,
            "limit": from_cents(limit),
            "limit_cents": limit,
            "spent": from_cents(used),
            "remaining": from_cents(limit - used),
            "pct": round(ratio, 2),
            "state": "over" if ratio > 100 else "warn" if ratio >= 80 else "ok",
            "color": palette.get(budget.category) or colour_for(index),
        })
    return status


def build_daily(db: Session, user_id: int, days: int) -> list[dict]:
    """Return one bucket per day for the trailing ``days`` window.

    Every day in the window is present, so days without activity appear as zero
    rather than being omitted.
    """
    today = dt_date.today()
    start = today - timedelta(days=days - 1)

    buckets: dict[str, dict] = {}
    for offset in range(days):
        cursor = start + timedelta(days=offset)
        buckets[cursor.isoformat()] = {
            "date": cursor.isoformat(),
            "income_cents": 0,
            "expenses_cents": 0,
        }

    for t in load_transactions(db, user_id, start=start):
        bucket = buckets.get(t.date.isoformat())
        if bucket is None:
            continue
        if t.type == "income":
            bucket["income_cents"] += t.amount_cents
        elif t.type == "expense":
            bucket["expenses_cents"] += t.amount_cents

    return [
        {
            "date": b["date"],
            "income": from_cents(b["income_cents"]),
            "expenses": from_cents(b["expenses_cents"]),
            "net": from_cents(b["income_cents"] - b["expenses_cents"]),
        }
        for b in buckets.values()
    ]


def build_summary(db: Session, user_id: int) -> dict:
    """Return the dashboard summary: all time totals, this month, and insights.

    Unclassified legacy rows, whose type is neither income nor expense, are
    counted in ``transaction_count`` but excluded from every monetary total.
    """
    everything = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    income, expenses = totals_cents(everything)
    balance = income - expenses
    classified = [t for t in everything if t.is_classified]

    today = dt_date.today()
    this_start = month_start(today)
    prev_start = add_months(this_start, -1)

    month_income, month_expenses = totals_cents(
        load_transactions(db, user_id, start=this_start)
    )
    prev_income, prev_expenses = totals_cents(
        load_transactions(db, user_id, start=prev_start, end=this_start)
    )

    elapsed = max(1, today.day)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projected = round(month_expenses / elapsed) * days_in_month

    savings_rate = round(balance / income * 100, 2) if income else 0.0
    month_savings_rate = (
        round((month_income - month_expenses) / month_income * 100, 2) if month_income else 0.0
    )
    prev_month_savings_rate = (
        round((prev_income - prev_expenses) / prev_income * 100, 2) if prev_income else 0.0
    )

    largest = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id, Transaction.type == "expense")
        .order_by(Transaction.amount_cents.desc())
        .first()
    )

    month_breakdown = build_breakdown(db, user_id, 1, "expense")
    top_category = month_breakdown[0] if month_breakdown else None

    biggest_jump = None
    prev_month_breakdown = {
        c["category"]: c["total_cents"] for c in _breakdown_for_month(db, user_id, prev_start)
    }
    for entry in month_breakdown:
        base = prev_month_breakdown.get(entry["category"], 0)
        delta = entry["total_cents"] - base
        if delta > 0 and (biggest_jump is None or delta > biggest_jump["delta_cents"]):
            biggest_jump = {
                "category": entry["category"],
                "delta": from_cents(delta),
                "delta_cents": delta,
                "pct": pct_change(entry["total_cents"], base),
            }

    accounts = account_rows(db, user_id)
    net_worth = sum(account["balance_cents"] for account in accounts)

    return {
        "generated_at": today.isoformat(),
        "balance": from_cents(balance),
        "balance_cents": balance,
        "accounts": accounts,
        "net_worth": from_cents(net_worth),
        "net_worth_cents": net_worth,
        "income": from_cents(income),
        "income_cents": income,
        "expenses": from_cents(expenses),
        "expenses_cents": expenses,
        "net": from_cents(balance),
        "savings_rate": savings_rate,
        "transaction_count": len(everything),
        "classified_count": len(classified),
        "unclassified_count": len(everything) - len(classified),
        "month": {
            "key": this_start.strftime("%Y-%m"),
            "label": month_label(this_start, short=False),
            "income": from_cents(month_income),
            "income_cents": month_income,
            "expenses": from_cents(month_expenses),
            "expenses_cents": month_expenses,
            "net": from_cents(month_income - month_expenses),
            "savings_rate": month_savings_rate,
            "prev_savings_rate": prev_month_savings_rate,
            "avg_daily_spend": from_cents(round(month_expenses / elapsed)),
            "projected_expenses": from_cents(projected),
            "days_elapsed": elapsed,
            "days_in_month": days_in_month,
            "income_delta": pct_change(month_income, prev_income),
            "expenses_delta": pct_change(month_expenses, prev_expenses),
        },
        "largest_expense": largest.to_dict() if largest else None,
        "top_category": top_category,
        "biggest_jump": biggest_jump,
        "budgets": build_budget_status(db, user_id, this_start, add_months(this_start, 1)),
    }


@app.get("/", include_in_schema=False)
def home(request: Request):
    """Serve the single page dashboard."""
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve the SVG favicon for browsers requesting the legacy path."""
    return FileResponse(BASE_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    """Report that the service is up, without exposing anyone's data.

    This endpoint stays public so the sign-in screen can tell a stopped server
    from a signed-out one, so it deliberately reports no row counts.
    """
    return {
        "status": "ok",
        "database": DB_FILE.name,
        "schema": migrations.SCHEMA_VERSION,
        "users": db.query(User).count(),
        "needs_setup": needs_setup(db),
    }


@app.get("/api/summary")
def summary(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Return the signed-in user's full dashboard summary."""
    return build_summary(db, user.id)


@app.get("/api/timeseries")
def timeseries(
    months: int = Query(6, ge=1, le=36),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Return monthly income, expense, and net totals."""
    return {"months": months, "series": build_timeseries(db, user.id, months)}


@app.get("/api/breakdown")
def breakdown(
    months: int = Query(6, ge=1, le=36),
    type: str = Query("expense"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Return per category totals for the trailing ``months`` window."""
    kind = type.strip().lower()
    if kind not in TRANSACTION_TYPES:
        raise HTTPException(422, f"type must be one of {TRANSACTION_TYPES}")
    return {
        "months": months,
        "type": kind,
        "categories": build_breakdown(db, user.id, months, kind),
    }


@app.get("/api/daily")
def daily(
    days: int = Query(30, ge=7, le=180),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Return per day totals for the trailing ``days`` window."""
    return {"days": days, "series": build_daily(db, user.id, days)}


@app.get("/api/transactions")
def list_transactions(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    type: str | None = Query(None),
    category: str | None = Query(None),
    account: str | None = Query(None),
    search: str | None = Query(None, max_length=140),
    date_from: dt_date | None = Query(None),
    date_to: dt_date | None = Query(None),
    sort: str = Query("date"),
    order: str = Query("desc"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Return a filtered, sorted, paginated page of transactions.

    Alongside the page, the response includes the total match count and the
    distinct category and account names in use, so the client can populate
    filter controls without a second request.
    """
    query = db.query(Transaction).filter(Transaction.user_id == user.id)

    if type and type.strip().lower() in TRANSACTION_TYPES:
        query = query.filter(Transaction.type == type.strip().lower())
    if category:
        query = query.filter(Transaction.category == category.strip())
    if account:
        query = query.filter(Transaction.account == account.strip())
    if search:
        needle = f"%{search.strip()}%"
        query = query.filter(
            Transaction.description.ilike(needle)
            | Transaction.category.ilike(needle)
            | Transaction.account.ilike(needle)
        )
    if date_from:
        query = query.filter(Transaction.date >= date_from)
    if date_to:
        query = query.filter(Transaction.date <= date_to)

    column = {
        "date": Transaction.date,
        "amount": Transaction.amount_cents,
        "category": Transaction.category,
        "account": Transaction.account,
        "id": Transaction.id,
    }.get(sort, Transaction.date)
    query = query.order_by(column.desc() if order == "desc" else column.asc())

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    everything = db.query(Transaction).filter(Transaction.user_id == user.id).all()
    used = {t.category for t in everything if t.is_classified}
    used_accounts = {t.account for t in everything if t.is_classified}

    return {
        "items": [t.to_dict() for t in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "categories": sorted(used),
        "accounts": sorted(used_accounts),
    }


def _write_transaction(
    db: Session,
    user_id: int,
    row: Transaction,
    payload: TransactionIn,
) -> Transaction:
    """Apply a validated payload to a transaction row without committing.

    Raises:
        HTTPException: With status 422 if the amount is unusable, or the category
            or account is not one this user defines.
    """
    try:
        cents = to_cents(payload.amount)
    except MoneyError as exc:
        raise HTTPException(422, str(exc)) from exc

    assert_category_known(db, user_id, payload.category)
    assert_account_known(db, user_id, payload.account)

    row.amount_cents = cents
    row.type = payload.type
    row.category = payload.category
    row.account = payload.account
    row.description = payload.description
    row.date = payload.date
    row.user_id = user_id
    return row


@app.post("/api/transactions", status_code=201)
def create_transaction(
    payload: TransactionIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Create a transaction and return the stored row."""
    row = _write_transaction(db, user.id, Transaction(), payload)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.put("/api/transactions/{transaction_id}")
def update_transaction(
    transaction_id: int,
    payload: TransactionIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Replace a transaction with the given payload and return the stored row.

    Raises:
        HTTPException: With status 404 if this user has no such transaction.
    """
    row = owned(Transaction, db, user.id, transaction_id, "Transaction")
    _write_transaction(db, user.id, row, payload)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(
    transaction_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Delete a transaction permanently.

    Raises:
        HTTPException: With status 404 if this user has no such transaction.
    """
    row = owned(Transaction, db, user.id, transaction_id, "Transaction")
    db.delete(row)
    db.commit()
    return {"deleted": transaction_id}


@app.get("/api/categories")
def list_categories(
    include_archived: bool = Query(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Return the user's categories, ordered by kind then user defined position.

    Archived categories are omitted unless ``include_archived`` is set.
    """
    query = db.query(Category).filter(Category.user_id == user.id)
    if not include_archived:
        query = query.filter(Category.is_archived.is_(False))
    rows = query.order_by(Category.kind, Category.sort_order, Category.name).all()
    return {"categories": [c.to_dict() for c in rows]}


@app.post("/api/categories", status_code=201)
def create_category(
    payload: CategoryIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Create a category and return it.

    Raises:
        HTTPException: With status 422 if the name is empty after cleaning, or
            409 if the user already has a category with that name.
    """
    name = " ".join(payload.name.split())[:40]
    if not name:
        raise HTTPException(422, "Category name cannot be empty")
    if db.query(Category).filter(
        Category.user_id == user.id, Category.name == name
    ).first():
        raise HTTPException(409, f"Category '{name}' already exists")

    count = db.query(Category).filter(Category.user_id == user.id).count()
    row = Category(
        name=name,
        kind=payload.kind,
        color=payload.color or colour_for(count),
        is_archived=payload.is_archived,
        sort_order=count,
        user_id=user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.put("/api/categories/{category_id}")
def update_category(
    category_id: int,
    payload: CategoryPatch,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Apply a partial update to a category and return it.

    Renaming a category rewrites the ``category`` text on every transaction and
    budget that references the old name, so history stays attached to the
    category the user recognises.

    Raises:
        HTTPException: With status 404 if this user has no such category, 422 if
            the new name is empty, or 409 if the new name is already taken.
    """
    row = owned(Category, db, user.id, category_id, "Category")

    if payload.name is not None:
        new_name = " ".join(payload.name.split())[:40]
        if not new_name:
            raise HTTPException(422, "Category name cannot be empty")
        clash = db.query(Category).filter(
            Category.user_id == user.id,
            Category.name == new_name,
            Category.id != row.id,
        ).first()
        if clash:
            raise HTTPException(409, f"Category '{new_name}' already exists")
        if new_name != row.name:
            for t in db.query(Transaction).filter(
                Transaction.user_id == user.id,
                Transaction.category == row.name,
            ).all():
                t.category = new_name
            for b in db.query(Budget).filter(
                Budget.user_id == user.id,
                Budget.category == row.name,
            ).all():
                b.category = new_name
            row.name = new_name

    if payload.kind is not None:
        row.kind = payload.kind
    if payload.color is not None:
        row.color = payload.color
    if payload.is_archived is not None:
        row.is_archived = payload.is_archived

    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.delete("/api/categories/{category_id}")
def delete_category(
    category_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Delete a category and any budget attached to it.

    Raises:
        HTTPException: With status 404 if this user has no such category, or 409
            if any of their transactions still reference it. Archiving is the
            non destructive alternative when history must be kept.
    """
    row = owned(Category, db, user.id, category_id, "Category")

    used = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.category == row.name,
    ).count()
    if used:
        raise HTTPException(
            409,
            f"'{row.name}' is used by {used} transaction(s). "
            "Archive it instead to keep your history intact.",
        )

    db.query(Budget).filter(
        Budget.user_id == user.id,
        Budget.category == row.name,
    ).delete()
    db.delete(row)
    db.commit()
    return {"deleted": row.name}


@app.get("/api/accounts")
def list_accounts(
    include_archived: bool = Query(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Return the user's accounts with balances, plus the overall net worth.

    Archived accounts are omitted unless ``include_archived`` is set. The
    ``net_worth_cents`` figure always spans every account, archived or not, so
    money parked in a closed account still counts towards the total.
    """
    rows = account_rows(db, user.id)
    net_worth = sum(row["balance_cents"] for row in rows)
    if not include_archived:
        rows = [row for row in rows if not row["is_archived"]]
    return {
        "accounts": rows,
        "net_worth": from_cents(net_worth),
        "net_worth_cents": net_worth,
    }


@app.post("/api/accounts", status_code=201)
def create_account(
    payload: AccountIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Create an account and return it.

    Raises:
        HTTPException: With status 422 if the name is empty after cleaning, or
            409 if the user already has an account with that name.
    """
    name = clean_label(payload.name)
    if not name:
        raise HTTPException(422, "Account name cannot be empty")
    name = normalize_label(name)
    if account_name_taken(db, user.id, name):
        raise HTTPException(409, f"Account '{name}' already exists")

    count = db.query(Account).filter(Account.user_id == user.id).count()
    row = Account(
        name=name,
        kind=payload.kind,
        color=payload.color or colour_for(count),
        opening_balance_cents=to_signed_cents(payload.opening_balance),
        is_archived=payload.is_archived,
        sort_order=count,
        user_id=user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return account_entry(db, user.id, row.id)


@app.put("/api/accounts/{account_id}")
def update_account(
    account_id: int,
    payload: AccountPatch,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Apply a partial update to an account and return it.

    Renaming an account rewrites the ``account`` text on every transaction that
    references the old name, so history stays attached to the account the user
    recognises.

    Raises:
        HTTPException: With status 404 if this user has no such account, 422 if
            the new name is empty, or 409 if the new name is already taken.
    """
    row = owned(Account, db, user.id, account_id, "Account")

    if payload.name is not None:
        new_name = clean_label(payload.name)
        if not new_name:
            raise HTTPException(422, "Account name cannot be empty")
        new_name = normalize_label(new_name)
        if account_name_taken(db, user.id, new_name, exclude_id=row.id):
            raise HTTPException(409, f"Account '{new_name}' already exists")
        if new_name != row.name:
            for t in db.query(Transaction).filter(
                Transaction.user_id == user.id,
                Transaction.account == row.name,
            ).all():
                t.account = new_name
            row.name = new_name

    if payload.kind is not None:
        row.kind = payload.kind
    if payload.color is not None:
        row.color = payload.color
    if payload.opening_balance is not None:
        row.opening_balance_cents = to_signed_cents(payload.opening_balance)
    if payload.is_archived is not None:
        row.is_archived = payload.is_archived
    if payload.sort_order is not None:
        row.sort_order = payload.sort_order

    db.commit()
    db.refresh(row)
    return account_entry(db, user.id, row.id)


@app.delete("/api/accounts/{account_id}")
def delete_account(
    account_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Delete one of the user's accounts.

    Raises:
        HTTPException: With status 404 if this user has no such account, or 409 if
            it is their only account or any of their transactions reference it.
            Archiving is the non destructive alternative when history must be
            kept.
    """
    row = owned(Account, db, user.id, account_id, "Account")

    if db.query(Account).filter(Account.user_id == user.id).count() <= 1:
        raise HTTPException(
            409,
            "Cannot delete the last account. Create another account first so "
            "the ledger always has somewhere to post.",
        )

    used = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.account == row.name,
    ).count()
    if used:
        raise HTTPException(
            409,
            f"'{row.name}' is used by {used} transaction(s). "
            "Archive it instead to keep your history intact.",
        )

    db.delete(row)
    db.commit()
    return {"deleted": row.name}


@app.get("/api/budgets")
def list_budgets(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Return the user's budgets with their spend for the current month."""
    today = dt_date.today()
    return {
        "budgets": build_budget_status(
            db, user.id, month_start(today), add_months(month_start(today), 1)
        )
    }


@app.put("/api/budgets/{category}")
def set_budget(
    category: str,
    payload: BudgetIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Create or update the monthly limit for a category and return it.

    Raises:
        HTTPException: With status 422 if the category is empty, not one this
            user defines, or the limit cannot be represented as cents.
    """
    name = " ".join(category.split())[:40]
    if not name:
        raise HTTPException(422, "Category cannot be empty")
    assert_category_known(db, user.id, name)

    try:
        limit = to_cents(payload.limit) if payload.limit > 0 else 0
    except MoneyError as exc:
        raise HTTPException(422, str(exc)) from exc

    row = db.query(Budget).filter(
        Budget.user_id == user.id,
        Budget.category == name,
    ).first()
    if row is None:
        row = Budget(category=name, limit_cents=limit, user_id=user.id)
        db.add(row)
    else:
        row.limit_cents = limit
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.delete("/api/budgets/{category}")
def delete_budget(
    category: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Remove the budget for a category.

    Raises:
        HTTPException: With status 404 if the user has no budget for that
            category.
    """
    name = " ".join(category.split())[:40]
    row = db.query(Budget).filter(
        Budget.user_id == user.id,
        Budget.category == name,
    ).first()
    if row is None:
        raise HTTPException(404, "Budget not found")
    db.delete(row)
    db.commit()
    return {"deleted": name}


@app.get("/api/export")
def export_data(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Return a complete, versioned JSON backup as a file download.

    The payload carries only the signed-in user's transactions, categories,
    budgets, and accounts together with exact cent amounts, so a round trip
    through :func:`import_data` is lossless and never leaks another person's
    ledger.
    """
    payload = {
        "app": "finlify",
        "export_version": EXPORT_VERSION,
        "exported_at": dt_date.today().isoformat(),
        "owner": user.username,
        "transactions": [
            t.to_dict() for t in db.query(Transaction).filter(
                Transaction.user_id == user.id).all()
        ],
        "categories": [
            c.to_dict() for c in db.query(Category).filter(
                Category.user_id == user.id).all()
        ],
        "budgets": [
            b.to_dict() for b in db.query(Budget).filter(
                Budget.user_id == user.id).all()
        ],
        "accounts": [
            a.to_dict() for a in db.query(Account).filter(
                Account.user_id == user.id).all()
        ],
    }
    stamp = dt_date.today().isoformat()
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="finlify-backup-{stamp}.json"'},
    )


class ImportIn(BaseModel):
    """Payload for restoring a JSON backup.

    With ``replace`` set, the current data is cleared first, which makes the
    import a restore. Otherwise rows are merged, and transactions that already
    exist are skipped, so re-importing the same backup is idempotent.
    """

    replace: bool = Field(default=False)
    transactions: list[dict] = Field(default_factory=list)
    categories: list[dict] = Field(default_factory=list)
    budgets: list[dict] = Field(default_factory=list)
    accounts: list[dict] = Field(default_factory=list)


def _imported_cents(item: dict, major_key: str = "amount", cents_key: str = "amount_cents") -> int:
    """Read an amount from an imported row, accepting major units or cents.

    The ``major_key`` value is read as major units. A row that carries only
    ``cents_key`` is read as an exact integer, never rescaled. Transactions use
    the default ``amount`` keys; budgets pass ``limit`` and ``limit_cents``.

    Raises:
        MoneyError: If neither key holds a usable value.
    """
    if item.get(major_key) is not None:
        return to_cents(item[major_key])
    if item.get(cents_key) is not None:
        return to_cents(Decimal(str(item[cents_key])) / 100)
    raise MoneyError("Amount is missing")


def _imported_signed_cents(
    item: dict,
    major_key: str = "opening_balance",
    cents_key: str = "opening_balance_cents",
) -> int:
    """Read a signed amount from an imported row, accepting major units or cents.

    Used for account opening balances, which may be zero or negative. A row that
    omits the value entirely contributes a zero balance.

    Raises:
        MoneyError: If a provided value cannot be represented as signed cents.
    """
    if item.get(major_key) is not None:
        return to_signed_cents(item[major_key])
    if item.get(cents_key) is not None:
        return to_signed_cents(Decimal(str(item[cents_key])) / 100)
    return 0


@app.post("/api/import")
def import_data(
    payload: ImportIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Import a JSON backup and return counts of what was applied.

    Accounts and categories are imported first so transactions can be validated
    against them, and a ``Main`` account is guaranteed to exist even for a
    legacy backup that predates accounts. A transaction whose date, type,
    category, account, amount, and description already exist is skipped, so
    repeated imports never duplicate money. Any row that is malformed,
    references an unknown category or account, or carries an unusable amount is
    also skipped and counted, rather than aborting the whole import.

    A colour that is not a hex value is not grounds for skipping: the row is
    kept and given a palette colour, because losing a whole category over a
    cosmetic field would cost the user real history.

    Every row is written against the signed-in user, so importing can never
    merge another person's ledger into this one, and replace mode only clears
    the signed-in user's own data.

    Args:
        payload: The backup to apply, optionally in replace mode.
        db: An open database session.

    Returns:
        Counts of created accounts, categories, transactions, and budgets, plus
        the number of skipped entries.
    """
    stats = {"categories": 0, "transactions": 0, "budgets": 0, "accounts": 0, "skipped": 0}

    if payload.replace:
        db.query(Transaction).filter(Transaction.user_id == user.id).delete()
        db.query(Category).filter(Category.user_id == user.id).delete()
        db.query(Budget).filter(Budget.user_id == user.id).delete()
        db.query(Account).filter(Account.user_id == user.id).delete()
        db.commit()

    seen_accounts: set[str] = set()
    for order, item in enumerate(payload.accounts):
        raw_name = clean_label(item.get("name", ""))
        if not raw_name:
            stats["skipped"] += 1
            continue
        name = normalize_label(raw_name)
        if name.lower() in seen_accounts or account_name_taken(db, user.id, name):
            stats["skipped"] += 1
            continue
        seen_accounts.add(name.lower())
        kind = str(item.get("kind", "checking")).lower()
        try:
            opening = _imported_signed_cents(item)
            sort_order = int(item.get("sort_order", order))
        except (MoneyError, TypeError, ValueError):
            stats["skipped"] += 1
            continue
        db.add(Account(
            name=name,
            kind=kind if kind in ACCOUNT_KINDS else "checking",
            color=normalize_color(item.get("color"), colour_for(order)),
            opening_balance_cents=opening,
            is_archived=bool(item.get("is_archived", False)),
            sort_order=sort_order,
            user_id=user.id,
        ))
        stats["accounts"] += 1
    db.flush()
    if db.query(Account).filter(Account.user_id == user.id).first() is None:
        db.add(Account(
            name="Main",
            kind="checking",
            color=CATEGORY_PALETTE[0],
            sort_order=0,
            user_id=user.id,
        ))
    db.commit()

    seen_categories: set[str] = set()
    for order, item in enumerate(payload.categories):
        raw_name = clean_label(item.get("name", ""))
        if not raw_name:
            stats["skipped"] += 1
            continue
        name = normalize_label(raw_name)
        if name in seen_categories or db.query(Category).filter(
            Category.user_id == user.id, Category.name == name
        ).first():
            stats["skipped"] += 1
            continue
        seen_categories.add(name)
        kind = str(item.get("kind", "expense")).lower()
        try:
            sort_order = int(item.get("sort_order", order))
        except (TypeError, ValueError):
            sort_order = order
        db.add(Category(
            name=name,
            kind=kind if kind in CATEGORY_KINDS else "expense",
            color=normalize_color(item.get("color"), colour_for(order)),
            is_archived=bool(item.get("is_archived", False)),
            sort_order=sort_order,
            user_id=user.id,
        ))
        stats["categories"] += 1
    db.commit()

    valid = known_category_names(db, user.id)
    valid_accounts = known_account_names(db, user.id)
    seen = {
        (t.date, t.type, t.category, t.account, t.amount_cents, t.description or None)
        for t in db.query(Transaction).filter(Transaction.user_id == user.id).all()
    }
    for item in payload.transactions:
        category = str(item.get("category", "")).strip()[:40]
        kind = str(item.get("type", "")).strip().lower()
        account = clean_label(item.get("account", "Main")) or "Main"
        if kind not in TRANSACTION_TYPES or category not in valid or account not in valid_accounts:
            stats["skipped"] += 1
            continue
        try:
            cents = _imported_cents(item)
            when = dt_date.fromisoformat(str(item["date"]))
        except (MoneyError, KeyError, TypeError, ValueError):
            stats["skipped"] += 1
            continue
        description = item.get("description") or None
        fingerprint = (when, kind, category, account, cents, description)
        if fingerprint in seen:
            stats["skipped"] += 1
            continue
        seen.add(fingerprint)
        db.add(Transaction(
            amount_cents=cents,
            type=kind,
            category=category,
            account=account,
            description=description,
            date=when,
            user_id=user.id,
        ))
        stats["transactions"] += 1

    for item in payload.budgets:
        category = str(item.get("category", "")).strip()[:40]
        if category not in valid:
            stats["skipped"] += 1
            continue
        try:
            limit = _imported_cents(item, "limit", "limit_cents")
        except MoneyError:
            stats["skipped"] += 1
            continue
        if db.query(Budget).filter(
            Budget.user_id == user.id,
            Budget.category == category,
        ).first() is None:
            db.add(Budget(category=category, limit_cents=limit, user_id=user.id))
            stats["budgets"] += 1

    db.commit()
    return stats


class CredentialsIn(BaseModel):
    """A username and password pair used to sign in."""

    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


class SetupIn(BaseModel):
    """The first-launch claim of an existing ledger by its administrator."""

    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=60)
    currency: str | None = Field(default=None, max_length=3)
    theme: str | None = None

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, v: str | None) -> str | None:
        """Require a supported ISO currency code."""
        if v is None:
            return None
        code = v.strip().upper()
        if code not in preferences.CURRENCY_CODES:
            raise ValueError(f"currency must be one of {preferences.CURRENCY_CODES}")
        return code

    @field_validator("theme")
    @classmethod
    def _valid_theme(cls, v: str | None) -> str | None:
        """Require one of the supported appearance themes."""
        if v is None:
            return None
        theme = v.strip().lower()
        if theme not in preferences.THEMES:
            raise ValueError(f"theme must be one of {preferences.THEMES}")
        return theme


class RegisterIn(BaseModel):
    """A request to create an additional local account."""

    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=60)


class SettingsIn(BaseModel):
    """A partial update of the signed-in user's own preferences."""

    display_name: str | None = Field(default=None, max_length=60)
    currency: str | None = Field(default=None, max_length=3)
    theme: str | None = None

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, v: str | None) -> str | None:
        """Require a supported ISO currency code."""
        if v is None:
            return None
        code = v.strip().upper()
        if code not in preferences.CURRENCY_CODES:
            raise ValueError(f"currency must be one of {preferences.CURRENCY_CODES}")
        return code

    @field_validator("theme")
    @classmethod
    def _valid_theme(cls, v: str | None) -> str | None:
        """Require one of the supported appearance themes."""
        if v is None:
            return None
        theme = v.strip().lower()
        if theme not in preferences.THEMES:
            raise ValueError(f"theme must be one of {preferences.THEMES}")
        return theme


class PasswordChangeIn(BaseModel):
    """A request to replace the signed-in user's own password."""

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class AdminUserIn(BaseModel):
    """A request for the administrator to create another local account."""

    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=60)


class AdminUserPatch(BaseModel):
    """A partial update the administrator may apply to any account."""

    display_name: str | None = Field(default=None, max_length=60)
    currency: str | None = Field(default=None, max_length=3)
    theme: str | None = None

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, v: str | None) -> str | None:
        """Require a supported ISO currency code."""
        if v is None:
            return None
        code = v.strip().upper()
        if code not in preferences.CURRENCY_CODES:
            raise ValueError(f"currency must be one of {preferences.CURRENCY_CODES}")
        return code

    @field_validator("theme")
    @classmethod
    def _valid_theme(cls, v: str | None) -> str | None:
        """Require one of the supported appearance themes."""
        if v is None:
            return None
        theme = v.strip().lower()
        if theme not in preferences.THEMES:
            raise ValueError(f"theme must be one of {preferences.THEMES}")
        return theme


class AdminPasswordIn(BaseModel):
    """A password the administrator is setting directly for another account."""

    password: str = Field(min_length=1, max_length=200)


@app.get("/api/auth/state")
def auth_state(
    user: User | None = Depends(signed_in_user),
    db: Session = Depends(get_db),
):
    """Report whether anyone is signed in and what the client should do next.

    This is the first call the interface makes, so it stays public and exposes
    nothing beyond sign-in state and the available appearance options.
    """
    auth.purge_expired(db)
    if user is None:
        return {
            "authenticated": False,
            "needs_setup": needs_setup(db),
            "user": None,
            "currencies": preferences.currency_options(),
            "themes": list(preferences.THEMES),
        }
    return {"authenticated": True, "needs_setup": False, **auth_payload(user)}


@app.post("/api/auth/setup", status_code=201)
def auth_setup(payload: SetupIn, response: Response, db: Session = Depends(get_db)):
    """Claim an unclaimed ledger by naming and passwording the administrator.

    A database that predates sign-in arrives here with an administrator that has
    no password, and this is the one request allowed to set it.

    Raises:
        HTTPException: With status 409 if the ledger has already been claimed, or
            if the requested username is taken.
    """
    admin = admin_user(db)
    if admin.has_password:
        raise HTTPException(409, "This ledger has already been claimed")
    admin.username = unique_username(db, payload.username, exclude_id=admin.id)
    if payload.display_name:
        admin.display_name = payload.display_name
    try:
        admin.password_hash = auth.hash_password(payload.password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    if payload.currency:
        admin.currency = payload.currency
    if payload.theme:
        admin.theme = payload.theme
    db.commit()
    set_session_cookie(response, auth.create_session(db, admin))
    return auth_payload(admin)


@app.post("/api/auth/login")
def auth_login(payload: CredentialsIn, response: Response, db: Session = Depends(get_db)):
    """Sign a user in and hand back a session cookie.

    Raises:
        HTTPException: With status 401 for an unknown username, a wrong
            password, or an account that has not set one yet. The message is
            identical in every case so the endpoint cannot be used to discover
            which usernames exist.
    """
    auth.purge_expired(db)
    user = auth.authenticate(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(401, "Incorrect username or password")
    set_session_cookie(response, auth.create_session(db, user))
    return auth_payload(user)


@app.post("/api/auth/register", status_code=201)
def auth_register(payload: RegisterIn, response: Response, db: Session = Depends(get_db)):
    """Create an additional local account with its own empty ledger.

    Raises:
        HTTPException: With status 409 if the ledger has not been claimed yet, or
            the username is taken, or 422 if the password is unusable.
    """
    if needs_setup(db):
        raise HTTPException(409, "Finish setting up the owner account first")
    name = unique_username(db, payload.username)
    try:
        password_hash = auth.hash_password(payload.password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    user = User(
        username=name,
        display_name=payload.display_name or "",
        password_hash=password_hash,
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    seed_new_user(db, user)
    set_session_cookie(response, auth.create_session(db, user))
    return auth_payload(user)


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """End the current session and clear its cookie."""
    auth.delete_session(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"signed_out": True}


@app.get("/api/settings")
def read_settings(user: User = Depends(current_user)):
    """Return the signed-in user's preferences and the available options."""
    return auth_payload(user)


@app.put("/api/settings")
def update_settings(
    payload: SettingsIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Update the signed-in user's display name, currency, or theme."""
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.currency is not None:
        user.currency = payload.currency
    if payload.theme is not None:
        user.theme = payload.theme
    db.commit()
    return auth_payload(user)


@app.post("/api/settings/password")
def change_password(
    payload: PasswordChangeIn,
    response: Response,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Replace the signed-in user's password, ending every other session.

    Raises:
        HTTPException: With status 401 if the current password is wrong, or 422
            if the replacement password is unusable.
    """
    if not auth.verify_password(payload.current_password, user.password_hash):
        raise HTTPException(401, "Your current password is not correct")
    try:
        user.password_hash = auth.hash_password(payload.new_password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    auth.delete_user_sessions(db, user.id)
    set_session_cookie(response, auth.create_session(db, user))
    return auth_payload(user)


@app.get("/api/users")
def list_users(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Return every local account, for the administrator's settings screen."""
    rows = db.query(User).order_by(User.id).all()
    return {"users": [u.to_dict() for u in rows]}


@app.post("/api/users", status_code=201)
def create_user(
    payload: AdminUserIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new local account on the owner's behalf.

    Raises:
        HTTPException: With status 409 if the username is taken, or 422 if the
            password is unusable.
    """
    name = unique_username(db, payload.username)
    try:
        password_hash = auth.hash_password(payload.password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    user = User(
        username=name,
        display_name=payload.display_name or "",
        password_hash=password_hash,
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    seed_new_user(db, user)
    return user.to_dict()


@app.put("/api/users/{user_id}")
def update_user(
    user_id: int,
    payload: AdminUserPatch,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update another account's preferences as the administrator.

    Raises:
        HTTPException: With status 404 if no such account exists.
    """
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(404, "User not found")
    if payload.display_name is not None:
        row.display_name = payload.display_name
    if payload.currency is not None:
        row.currency = payload.currency
    if payload.theme is not None:
        row.theme = payload.theme
    db.commit()
    return row.to_dict()


@app.post("/api/users/{user_id}/password")
def set_user_password(
    user_id: int,
    payload: AdminPasswordIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set a new password for any account, ending that account's sessions.

    Raises:
        HTTPException: With status 404 if no such account exists, or 422 if the
            password is unusable.
    """
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(404, "User not found")
    try:
        row.password_hash = auth.hash_password(payload.password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    auth.delete_user_sessions(db, row.id)
    return row.to_dict()


@app.delete("/api/users/{user_id}")
def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete another account and everything it owns.

    The administrator is never deletable, and neither is the last remaining
    account, so the ledger always keeps someone who can reach settings.

    Raises:
        HTTPException: With status 404 if no such account exists, or 409 if the
            target is the administrator, the caller's own account, or the last
            one left.
    """
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(404, "User not found")
    if row.is_admin:
        raise HTTPException(409, "The administrator account cannot be deleted")
    if row.id == admin.id:
        raise HTTPException(409, "You cannot delete the account you are signed in with")
    if db.query(User).count() <= 1:
        raise HTTPException(409, "Cannot delete the last account")

    for model in (Transaction, Category, Budget, Account, AuthSession):
        db.query(model).filter(model.user_id == row.id).delete()
    db.delete(row)
    db.commit()
    return {"deleted": row.username}


@app.get("/dashboard", include_in_schema=False)
def legacy_dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Return a minimal totals payload for older bookmarks and scripts."""
    data = build_summary(db, user.id)
    return {
        "balance": data["balance"],
        "income": data["income"],
        "expenses": data["expenses"],
        "transaction_count": data["transaction_count"],
    }
