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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

import migrations
from database import Base, SessionLocal, engine
from models import CATEGORY_KINDS, CATEGORY_PALETTE, TRANSACTION_TYPES, Budget, Category, Transaction
from money import MoneyError, from_cents, to_cents

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "finlify.db"
EXPORT_VERSION = 1

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


def known_category_names(db: Session) -> set[str]:
    """Return the names of every category currently defined."""
    return {row.name for row in db.query(Category).all()}


def assert_category_known(db: Session, name: str) -> str:
    """Return ``name`` if it is a defined category.

    Raises:
        HTTPException: With status 422 if no such category exists, telling the
            caller to create it first.
    """
    if name not in known_category_names(db):
        raise HTTPException(422, f"Unknown category '{name}'. Create it first.")
    return name


class TransactionIn(BaseModel):
    """Payload for creating or updating a transaction.

    ``amount`` is expressed in major units, for example ``19.99``. It is
    validated here and stored as integer cents.
    """

    amount: Decimal = Field(description="Major units, e.g. 19.99")
    type: str
    category: str = Field(min_length=1, max_length=40)
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
        """Require a six digit hex colour, or allow ``None`` for a default."""
        if v is None:
            return None
        value = v.strip().lower()
        if len(value) == 7 and value.startswith("#") and all(
            c in "0123456789abcdef" for c in value[1:]
        ):
            return value
        raise ValueError("color must be a hex value like #5eead4")


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
        """Require a six digit hex colour, or allow ``None`` to leave it alone."""
        if v is None:
            return None
        value = v.strip().lower()
        if len(value) == 7 and value.startswith("#") and all(
            c in "0123456789abcdef" for c in value[1:]
        ):
            return value
        raise ValueError("color must be a hex value like #5eead4")


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


def load_transactions(db: Session, start: dt_date | None = None, end: dt_date | None = None):
    """Return transactions in ascending date order, optionally date filtered.

    Args:
        db: An open database session.
        start: Inclusive lower bound on the transaction date.
        end: Exclusive upper bound on the transaction date.
    """
    query = db.query(Transaction)
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


def build_timeseries(db: Session, months: int) -> list[dict]:
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

    for t in load_transactions(db, start=first):
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


def build_breakdown(db: Session, months: int, kind: str) -> list[dict]:
    """Return per category totals for the given type over a trailing window.

    Results are ordered by total descending, with each entry carrying its
    percentage share of the overall total.
    """
    today = dt_date.today()
    start = add_months(month_start(today), -(months - 1))

    totals: dict[str, dict] = {}
    for t in load_transactions(db, start=start):
        if t.type != kind:
            continue
        entry = totals.setdefault(t.category, {"total_cents": 0, "count": 0})
        entry["total_cents"] += t.amount_cents
        entry["count"] += 1

    grand = sum(e["total_cents"] for e in totals.values())
    palette = {c.name: c.color for c in db.query(Category).all()}
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


def _breakdown_for_month(db: Session, month: dt_date) -> list[dict]:
    """Return expense totals for one calendar month only.

    Used to compare consecutive months, which the range based
    :func:`build_breakdown` cannot do.
    """
    start = month_start(month)
    end = add_months(start, 1)
    totals: dict[str, int] = {}
    for t in load_transactions(db, start=start, end=end):
        if t.type == "expense":
            totals[t.category] = totals.get(t.category, 0) + t.amount_cents
    return [
        {"category": name, "total_cents": value}
        for name, value in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]


def build_budget_status(db: Session, start: dt_date, end: dt_date) -> list[dict]:
    """Return each budget with its spend for the given period and a state.

    The state is ``ok`` below 80 percent used, ``warn`` from 80 to 100 percent,
    and ``over`` above 100 percent.
    """
    budgets = db.query(Budget).order_by(Budget.category).all()
    if not budgets:
        return []

    spent: dict[str, int] = {}
    for t in load_transactions(db, start=start, end=end):
        if t.type == "expense":
            spent[t.category] = spent.get(t.category, 0) + t.amount_cents

    palette = {c.name: c.color for c in db.query(Category).all()}
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


def build_daily(db: Session, days: int) -> list[dict]:
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

    for t in load_transactions(db, start=start):
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


def build_summary(db: Session) -> dict:
    """Return the dashboard summary: all time totals, this month, and insights.

    Unclassified legacy rows, whose type is neither income nor expense, are
    counted in ``transaction_count`` but excluded from every monetary total.
    """
    everything = db.query(Transaction).all()
    income, expenses = totals_cents(everything)
    balance = income - expenses
    classified = [t for t in everything if t.is_classified]

    today = dt_date.today()
    this_start = month_start(today)
    prev_start = add_months(this_start, -1)

    month_income, month_expenses = totals_cents(load_transactions(db, start=this_start))
    prev_income, prev_expenses = totals_cents(load_transactions(db, start=prev_start, end=this_start))

    elapsed = max(1, today.day)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projected = round(month_expenses / elapsed) * days_in_month

    savings_rate = round(balance / income * 100, 2) if income else 0.0
    month_savings_rate = (
        round((month_income - month_expenses) / month_income * 100, 2) if month_income else 0.0
    )

    largest = (
        db.query(Transaction)
        .filter(Transaction.type == "expense")
        .order_by(Transaction.amount_cents.desc())
        .first()
    )

    month_breakdown = build_breakdown(db, 1, "expense")
    top_category = month_breakdown[0] if month_breakdown else None

    biggest_jump = None
    prev_month_breakdown = {
        c["category"]: c["total_cents"] for c in _breakdown_for_month(db, prev_start)
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

    return {
        "generated_at": today.isoformat(),
        "balance": from_cents(balance),
        "balance_cents": balance,
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
        "budgets": build_budget_status(db, this_start, add_months(this_start, 1)),
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
    """Report service health along with the database path and row counts."""
    return {
        "status": "ok",
        "database": str(DB_FILE),
        "transactions": db.query(Transaction).count(),
        "categories": db.query(Category).count(),
        "budgets": db.query(Budget).count(),
    }


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)):
    """Return the full dashboard summary."""
    return build_summary(db)


@app.get("/api/timeseries")
def timeseries(months: int = Query(6, ge=1, le=36), db: Session = Depends(get_db)):
    """Return monthly income, expense, and net totals."""
    return {"months": months, "series": build_timeseries(db, months)}


@app.get("/api/breakdown")
def breakdown(
    months: int = Query(6, ge=1, le=36),
    type: str = Query("expense"),
    db: Session = Depends(get_db),
):
    """Return per category totals for the trailing ``months`` window."""
    kind = type.strip().lower()
    if kind not in TRANSACTION_TYPES:
        raise HTTPException(422, f"type must be one of {TRANSACTION_TYPES}")
    return {"months": months, "type": kind, "categories": build_breakdown(db, months, kind)}


@app.get("/api/daily")
def daily(days: int = Query(30, ge=7, le=180), db: Session = Depends(get_db)):
    """Return per day totals for the trailing ``days`` window."""
    return {"days": days, "series": build_daily(db, days)}


@app.get("/api/transactions")
def list_transactions(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    type: str | None = Query(None),
    category: str | None = Query(None),
    search: str | None = Query(None, max_length=140),
    date_from: dt_date | None = Query(None),
    date_to: dt_date | None = Query(None),
    sort: str = Query("date"),
    order: str = Query("desc"),
    db: Session = Depends(get_db),
):
    """Return a filtered, sorted, paginated page of transactions.

    Alongside the page, the response includes the total match count and the
    distinct category names in use, so the client can populate a filter control
    without a second request.
    """
    query = db.query(Transaction)

    if type and type.strip().lower() in TRANSACTION_TYPES:
        query = query.filter(Transaction.type == type.strip().lower())
    if category:
        query = query.filter(Transaction.category == category.strip())
    if search:
        needle = f"%{search.strip()}%"
        query = query.filter(
            Transaction.description.ilike(needle) | Transaction.category.ilike(needle)
        )
    if date_from:
        query = query.filter(Transaction.date >= date_from)
    if date_to:
        query = query.filter(Transaction.date <= date_to)

    column = {
        "date": Transaction.date,
        "amount": Transaction.amount_cents,
        "category": Transaction.category,
        "id": Transaction.id,
    }.get(sort, Transaction.date)
    query = query.order_by(column.desc() if order == "desc" else column.asc())

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    used = {t.category for t in db.query(Transaction).all() if t.is_classified}

    return {
        "items": [t.to_dict() for t in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "categories": sorted(used),
    }


def _write_transaction(db: Session, row: Transaction, payload: TransactionIn) -> Transaction:
    """Apply a validated payload to a transaction row without committing.

    Raises:
        HTTPException: With status 422 if the amount is unusable or the
            category is not defined.
    """
    try:
        cents = to_cents(payload.amount)
    except MoneyError as exc:
        raise HTTPException(422, str(exc)) from exc

    assert_category_known(db, payload.category)

    row.amount_cents = cents
    row.type = payload.type
    row.category = payload.category
    row.description = payload.description
    row.date = payload.date
    return row


@app.post("/api/transactions", status_code=201)
def create_transaction(payload: TransactionIn, db: Session = Depends(get_db)):
    """Create a transaction and return the stored row."""
    row = _write_transaction(db, Transaction(), payload)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.put("/api/transactions/{transaction_id}")
def update_transaction(
    transaction_id: int, payload: TransactionIn, db: Session = Depends(get_db)
):
    """Replace a transaction with the given payload and return the stored row.

    Raises:
        HTTPException: With status 404 if no transaction has that id.
    """
    row = db.get(Transaction, transaction_id)
    if row is None:
        raise HTTPException(404, "Transaction not found")
    _write_transaction(db, row, payload)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(transaction_id: int, db: Session = Depends(get_db)):
    """Delete a transaction permanently.

    Raises:
        HTTPException: With status 404 if no transaction has that id.
    """
    row = db.get(Transaction, transaction_id)
    if row is None:
        raise HTTPException(404, "Transaction not found")
    db.delete(row)
    db.commit()
    return {"deleted": transaction_id}


@app.get("/api/categories")
def list_categories(
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Return all categories, ordered by kind then user defined position.

    Archived categories are omitted unless ``include_archived`` is set.
    """
    query = db.query(Category)
    if not include_archived:
        query = query.filter(Category.is_archived.is_(False))
    rows = query.order_by(Category.kind, Category.sort_order, Category.name).all()
    return {"categories": [c.to_dict() for c in rows]}


@app.post("/api/categories", status_code=201)
def create_category(payload: CategoryIn, db: Session = Depends(get_db)):
    """Create a category and return it.

    Raises:
        HTTPException: With status 422 if the name is empty after cleaning, or
            409 if a category with that name already exists.
    """
    name = " ".join(payload.name.split())[:40]
    if not name:
        raise HTTPException(422, "Category name cannot be empty")
    if db.query(Category).filter(Category.name == name).first():
        raise HTTPException(409, f"Category '{name}' already exists")

    count = db.query(Category).count()
    row = Category(
        name=name,
        kind=payload.kind,
        color=payload.color or colour_for(count),
        is_archived=payload.is_archived,
        sort_order=count,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.put("/api/categories/{category_id}")
def update_category(category_id: int, payload: CategoryPatch, db: Session = Depends(get_db)):
    """Apply a partial update to a category and return it.

    Renaming a category rewrites the ``category`` text on every transaction and
    budget that references the old name, so history stays attached to the
    category the user recognises.

    Raises:
        HTTPException: With status 404 if the category does not exist, 422 if
            the new name is empty, or 409 if the new name is already taken.
    """
    row = db.get(Category, category_id)
    if row is None:
        raise HTTPException(404, "Category not found")

    if payload.name is not None:
        new_name = " ".join(payload.name.split())[:40]
        if not new_name:
            raise HTTPException(422, "Category name cannot be empty")
        clash = db.query(Category).filter(Category.name == new_name, Category.id != row.id).first()
        if clash:
            raise HTTPException(409, f"Category '{new_name}' already exists")
        if new_name != row.name:
            for t in db.query(Transaction).filter(Transaction.category == row.name).all():
                t.category = new_name
            for b in db.query(Budget).filter(Budget.category == row.name).all():
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
def delete_category(category_id: int, db: Session = Depends(get_db)):
    """Delete a category and any budget attached to it.

    Raises:
        HTTPException: With status 404 if the category does not exist, or 409 if
            any transaction still references it. Archiving is the non
            destructive alternative when history must be kept.
    """
    row = db.get(Category, category_id)
    if row is None:
        raise HTTPException(404, "Category not found")

    used = db.query(Transaction).filter(Transaction.category == row.name).count()
    if used:
        raise HTTPException(
            409,
            f"'{row.name}' is used by {used} transaction(s). "
            "Archive it instead to keep your history intact.",
        )

    db.query(Budget).filter(Budget.category == row.name).delete()
    db.delete(row)
    db.commit()
    return {"deleted": row.name}


@app.get("/api/budgets")
def list_budgets(db: Session = Depends(get_db)):
    """Return every budget with its spend for the current month."""
    today = dt_date.today()
    return {
        "budgets": build_budget_status(db, month_start(today), add_months(month_start(today), 1))
    }


@app.put("/api/budgets/{category}")
def set_budget(category: str, payload: BudgetIn, db: Session = Depends(get_db)):
    """Create or update the monthly limit for a category and return it.

    Raises:
        HTTPException: With status 422 if the category is empty, not defined, or
            the limit cannot be represented as cents.
    """
    name = " ".join(category.split())[:40]
    if not name:
        raise HTTPException(422, "Category cannot be empty")
    assert_category_known(db, name)

    try:
        limit = to_cents(payload.limit) if payload.limit > 0 else 0
    except MoneyError as exc:
        raise HTTPException(422, str(exc)) from exc

    row = db.query(Budget).filter(Budget.category == name).first()
    if row is None:
        row = Budget(category=name, limit_cents=limit)
        db.add(row)
    else:
        row.limit_cents = limit
    db.commit()
    db.refresh(row)
    return row.to_dict()


@app.delete("/api/budgets/{category}")
def delete_budget(category: str, db: Session = Depends(get_db)):
    """Remove the budget for a category.

    Raises:
        HTTPException: With status 404 if no budget exists for that category.
    """
    name = " ".join(category.split())[:40]
    row = db.query(Budget).filter(Budget.category == name).first()
    if row is None:
        raise HTTPException(404, "Budget not found")
    db.delete(row)
    db.commit()
    return {"deleted": name}


@app.get("/api/export")
def export_data(db: Session = Depends(get_db)):
    """Return a complete, versioned JSON backup as a file download.

    The payload carries every transaction, category, and budget together with
    exact cent amounts, so a round trip through :func:`import_data` is lossless.
    """
    payload = {
        "app": "finlify",
        "export_version": EXPORT_VERSION,
        "exported_at": dt_date.today().isoformat(),
        "transactions": [t.to_dict() for t in db.query(Transaction).all()],
        "categories": [c.to_dict() for c in db.query(Category).all()],
        "budgets": [b.to_dict() for b in db.query(Budget).all()],
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


def _imported_cents(item: dict) -> int:
    """Read an amount from an imported row, accepting major units or cents.

    An ``amount`` key is read as major units. A row that carries only
    ``amount_cents`` is read as an exact integer, never rescaled.

    Raises:
        MoneyError: If neither key holds a usable value.
    """
    if item.get("amount") is not None:
        return to_cents(item["amount"])
    if item.get("amount_cents") is not None:
        return to_cents(Decimal(str(item["amount_cents"])) / 100)
    raise MoneyError("Amount is missing")


@app.post("/api/import")
def import_data(payload: ImportIn, db: Session = Depends(get_db)):
    """Import a JSON backup and return counts of what was applied.

    Categories are imported first so transactions can be validated against them.
    A transaction whose date, type, category, amount, and description already
    exist is skipped, so repeated imports never duplicate money. Any row that is
    malformed, references an unknown category, or carries an unusable amount is
    also skipped and counted, rather than aborting the whole import.

    Args:
        payload: The backup to apply, optionally in replace mode.
        db: An open database session.

    Returns:
        Counts of created categories, transactions, and budgets, plus the
        number of skipped entries.
    """
    stats = {"categories": 0, "transactions": 0, "budgets": 0, "skipped": 0}

    if payload.replace:
        db.query(Transaction).delete()
        db.query(Category).delete()
        db.query(Budget).delete()
        db.commit()

    for item in payload.categories:
        name = str(item.get("name", "")).strip()[:40]
        if not name or db.query(Category).filter(Category.name == name).first():
            stats["skipped"] += 1
            continue
        kind = str(item.get("kind", "expense")).lower()
        db.add(Category(
            name=name,
            kind=kind if kind in CATEGORY_KINDS else "expense",
            color=item.get("color") or colour_for(db.query(Category).count()),
            is_archived=bool(item.get("is_archived", False)),
            sort_order=db.query(Category).count(),
        ))
        stats["categories"] += 1
    db.commit()

    valid = known_category_names(db)
    seen = {
        (t.date, t.type, t.category, t.amount_cents, t.description or None)
        for t in db.query(Transaction).all()
    }
    for item in payload.transactions:
        category = str(item.get("category", "")).strip()[:40]
        kind = str(item.get("type", "")).strip().lower()
        if kind not in TRANSACTION_TYPES or category not in valid:
            stats["skipped"] += 1
            continue
        try:
            cents = _imported_cents(item)
            when = dt_date.fromisoformat(str(item["date"]))
        except (MoneyError, KeyError, TypeError, ValueError):
            stats["skipped"] += 1
            continue
        description = item.get("description") or None
        fingerprint = (when, kind, category, cents, description)
        if fingerprint in seen:
            stats["skipped"] += 1
            continue
        seen.add(fingerprint)
        db.add(Transaction(
            amount_cents=cents,
            type=kind,
            category=category,
            description=description,
            date=when,
        ))
        stats["transactions"] += 1

    for item in payload.budgets:
        category = str(item.get("category", "")).strip()[:40]
        if category not in valid:
            stats["skipped"] += 1
            continue
        try:
            limit = _imported_cents(item)
        except MoneyError:
            stats["skipped"] += 1
            continue
        if db.query(Budget).filter(Budget.category == category).first() is None:
            db.add(Budget(category=category, limit_cents=limit))
            stats["budgets"] += 1

    db.commit()
    return stats


@app.get("/dashboard", include_in_schema=False)
def legacy_dashboard(db: Session = Depends(get_db)):
    """Return a minimal totals payload for older bookmarks and scripts."""
    data = build_summary(db)
    return {
        "balance": data["balance"],
        "income": data["income"],
        "expenses": data["expenses"],
        "transaction_count": data["transaction_count"],
    }
