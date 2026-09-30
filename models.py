"""SQLAlchemy ORM models for Finlify.

Money is stored and aggregated exclusively as integer minor units (cents). A
float amount column must never be reintroduced: binary floating point cannot
represent 0.10 or 19.99 exactly, and repeated sums drift. Floats appear only in
:meth:`Transaction.to_dict` output, for JSON display.
"""

from datetime import date as dt_date

from sqlalchemy import Boolean, Column, Date, Integer, String, UniqueConstraint
from sqlalchemy.orm import validates

from database import Base
from preferences import DEFAULT_CURRENCY, DEFAULT_THEME, THEMES

TRANSACTION_TYPES = ("income", "expense")
CATEGORY_KINDS = ("income", "expense")
ACCOUNT_KINDS = ("checking", "savings", "credit", "cash", "investment")

ADMIN_USERNAME = "admin"

ADMIN_USER_ID = 1

CATEGORY_PALETTE = (
    "#5eead4", "#a78bfa", "#f0abfc", "#bef264", "#fbbf24", "#fb7185",
    "#60a5fa", "#2dd4bf", "#c084fc", "#fde047", "#4ade80", "#fb923c",
)


def normalize_label(value: str) -> str:
    """Normalise a user-facing label such as a category or account name.

    Whitespace is collapsed and the value is truncated to 40 characters. Title
    casing is applied only when the input is entirely lowercase, so names such
    as ``iPhone`` or ``ATMs`` keep the casing the user typed.
    """
    cleaned = " ".join(str(value).split())[:40]
    if not cleaned:
        raise ValueError("Name cannot be empty")
    if cleaned == cleaned.lower():
        return cleaned[:1].upper() + cleaned[1:]
    return cleaned


class User(Base):
    """A person who can sign in to Finlify on this machine.

    Every other table that holds user data carries a ``user_id`` pointing here,
    so one ledger belongs to exactly one person. The first user created by a
    migration is flagged ``is_admin`` and can never be deleted, which guarantees
    the database always has someone who can reach settings.

    ``password_hash`` is ``None`` until the person chooses a password, which is
    how a freshly migrated database asks the owner to claim their ledger.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False, unique=True, index=True)
    display_name = Column(String, nullable=False, default="")
    password_hash = Column(String, nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    currency = Column(String, nullable=False, default=DEFAULT_CURRENCY)
    theme = Column(String, nullable=False, default=DEFAULT_THEME)
    created_at = Column(Date, nullable=False, default=dt_date.today)

    @validates("username")
    def _clean_username(self, _key, value: str) -> str:
        """Lowercase the username so sign-in is not case sensitive."""
        return str(value).strip().lower()

    @validates("display_name")
    def _clean_display_name(self, _key, value: str) -> str:
        """Trim the display name and cap its length."""
        return " ".join(str(value or "").split())[:60]

    @validates("theme")
    def _check_theme(self, _key, value: str) -> str:
        """Validate that the theme is one of :data:`preferences.THEMES`."""
        theme = str(value).strip().lower()
        if theme not in THEMES:
            raise ValueError(f"theme must be one of {THEMES}")
        return theme

    @validates("currency")
    def _check_currency(self, _key, value: str) -> str:
        """Uppercase the ISO currency code so formatting stays predictable."""
        return str(value).strip().upper()

    @property
    def label(self) -> str:
        """The friendliest available name for this person."""
        return self.display_name or self.username

    @property
    def has_password(self) -> bool:
        """Whether a password has been set and the account can be signed into."""
        return bool(self.password_hash)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation, never including the hash."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "label": self.label,
            "is_admin": bool(self.is_admin),
            "has_password": self.has_password,
            "currency": self.currency,
            "theme": self.theme,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AuthSession(Base):
    """A signed-in browser session, stored server side.

    Only the SHA-256 of the cookie token is kept, so a leaked database cannot be
    replayed as a session. Expiry is a unix timestamp in seconds.
    """

    __tablename__ = "auth_sessions"

    token_hash = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    created_at = Column(Integer, nullable=False)
    expires_at = Column(Integer, nullable=False)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation of the session."""
        return {
            "user_id": self.user_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class Transaction(Base):
    """A single income or expense entry.

    ``category`` and ``account`` are stored as text rather than foreign keys so
    that renaming or archiving never rewrites history. Referential integrity is
    enforced in the API layer against the :class:`Category` and :class:`Account`
    tables.
    """

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    amount_cents = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    category = Column(String, nullable=False)
    account = Column(String, nullable=False, default="Main", index=True)
    description = Column(String, nullable=True)
    date = Column(Date, nullable=False)
    user_id = Column(Integer, nullable=False, default=ADMIN_USER_ID, index=True)

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
            "account": self.account,
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
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_categories_user_name"),)

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False, default="expense")
    color = Column(String, nullable=False, default=CATEGORY_PALETTE[0])
    is_archived = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)
    user_id = Column(Integer, nullable=False, default=ADMIN_USER_ID, index=True)

    @validates("name")
    def _clean_name(self, _key, value: str) -> str:
        """Normalise a category name, preserving deliberate capitalisation."""
        return normalize_label(value)

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


class Account(Base):
    """A wallet, bank account, or card that transactions are filed against.

    Transactions reference an account by name, so a rename cascades in the API
    layer while history stays intact. Every database is seeded with a ``Main``
    account so that the ledger always has somewhere to post.

    ``opening_balance_cents`` is a signed integer: it may be zero or negative
    (a credit card can open in the red) and is the anchor for the account's
    running balance, which is ``opening_balance_cents`` plus the signed sum of
    its transactions.
    """

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_accounts_user_name"),)

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False, default="checking")
    color = Column(String, nullable=False, default=CATEGORY_PALETTE[0])
    opening_balance_cents = Column(Integer, nullable=False, default=0)
    is_archived = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)
    user_id = Column(Integer, nullable=False, default=ADMIN_USER_ID, index=True)

    @validates("name")
    def _clean_name(self, _key, value: str) -> str:
        """Normalise an account name, preserving deliberate capitalisation."""
        return normalize_label(value)

    @validates("kind")
    def _check_kind(self, _key, value: str) -> str:
        """Validate that the kind is one of :data:`ACCOUNT_KINDS`."""
        kind = str(value).strip().lower()
        if kind not in ACCOUNT_KINDS:
            raise ValueError(f"kind must be one of {ACCOUNT_KINDS}")
        return kind

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation, including exact cents."""
        from money import from_cents

        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "color": self.color,
            "opening_balance": from_cents(self.opening_balance_cents),
            "opening_balance_cents": self.opening_balance_cents,
            "is_archived": bool(self.is_archived),
            "sort_order": self.sort_order,
        }


class Budget(Base):
    """A monthly spending limit for one expense category."""

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_budgets_user_category"),)

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String, nullable=False, index=True)
    limit_cents = Column(Integer, nullable=False, default=0)
    user_id = Column(Integer, nullable=False, default=ADMIN_USER_ID, index=True)

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
