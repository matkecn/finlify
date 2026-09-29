"""SQLAlchemy ORM models for Finlify.

Money is stored and aggregated exclusively as integer minor units (cents). A
float amount column must never be reintroduced: binary floating point cannot
represent 0.10 or 19.99 exactly, and repeated sums drift. Floats appear only in
:meth:`Transaction.to_dict` output, for JSON display.
"""

from sqlalchemy import Boolean, Column, Date, Integer, String
from sqlalchemy.orm import validates

from database import Base

TRANSACTION_TYPES = ("income", "expense")
CATEGORY_KINDS = ("income", "expense")

CATEGORY_PALETTE = (
    "#5eead4", "#a78bfa", "#f0abfc", "#bef264", "#fbbf24", "#fb7185",
    "#60a5fa", "#2dd4bf", "#c084fc", "#fde047", "#4ade80", "#fb923c",
)


class Transaction(Base):
    """A single income or expense entry.

    ``category`` is stored as text rather than a foreign key so that renaming or
    archiving a category never rewrites history. Referential integrity is
    enforced in the API layer against the :class:`Category` table.
    """

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    amount_cents = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    category = Column(String, nullable=False)
    description = Column(String, nullable=True)
    date = Column(Date, nullable=False)

    @property
    def is_classified(self) -> bool:
        """Whether this row has a recognised type and may be counted in totals."""
        return self.type in TRANSACTION_TYPES

    @property
    def signed_cents(self) -> int:
        """The amount in cents, positive for income and negative for expense.

        Unclassified rows return ``0`` so that legacy data cannot distort a
        balance.
        """
        if self.type == "income":
            return self.amount_cents
        if self.type == "expense":
            return -self.amount_cents
        return 0

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation, including exact cents."""
        from money import from_cents

        return {
            "id": self.id,
            "amount": from_cents(self.amount_cents),
            "amount_cents": self.amount_cents,
            "type": self.type,
            "category": self.category,
            "description": self.description,
            "date": self.date.isoformat() if self.date else None,
            "classified": self.is_classified,
        }


class Category(Base):
    """A user-managed category, either for income or for expenses.

    Transactions and budgets reference a category by name, so historical rows
    survive a rename, an archive, or a deletion that leaves no references.
    """

    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True, index=True)
    kind = Column(String, nullable=False, default="expense")
    color = Column(String, nullable=False, default=CATEGORY_PALETTE[0])
    is_archived = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)

    @validates("name")
    def _clean_name(self, _key, value: str) -> str:
        """Normalise a category name, preserving deliberate capitalisation.

        Whitespace is collapsed and the value is truncated to 40 characters.
        Title casing is applied only when the input is entirely lowercase, so
        names such as ``iPhone`` or ``ATMs`` keep the casing the user typed.
        """
        cleaned = " ".join(str(value).split())[:40]
        if not cleaned:
            raise ValueError("Category name cannot be empty")
        if cleaned == cleaned.lower():
            return cleaned[:1].upper() + cleaned[1:]
        return cleaned

    @validates("kind")
    def _check_kind(self, _key, value: str) -> str:
        """Validate that the kind is one of :data:`CATEGORY_KINDS`."""
        kind = str(value).strip().lower()
        if kind not in CATEGORY_KINDS:
            raise ValueError(f"kind must be one of {CATEGORY_KINDS}")
        return kind

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation of this category."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "color": self.color,
            "is_archived": bool(self.is_archived),
            "sort_order": self.sort_order,
        }


class Budget(Base):
    """A monthly spending limit for one expense category."""

    __tablename__ = "budgets"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String, nullable=False, unique=True, index=True)
    limit_cents = Column(Integer, nullable=False, default=0)

    @validates("category")
    def _clean_category(self, _key, value: str) -> str:
        """Normalise a category reference, collapsing whitespace."""
        cleaned = " ".join(str(value).split())[:40]
        if not cleaned:
            raise ValueError("Category cannot be empty")
        return cleaned

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation, including exact cents."""
        from money import from_cents

        return {
            "id": self.id,
            "category": self.category,
            "limit": from_cents(self.limit_cents),
            "limit_cents": self.limit_cents,
        }


class AppMeta(Base):
    """A key/value store for schema versioning and other bookkeeping."""

    __tablename__ = "app_meta"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
