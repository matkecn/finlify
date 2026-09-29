"""Database engine, session factory, and declarative base for Finlify.

The SQLite file lives next to the application by default and can be relocated
with the ``FINLIFY_DATA_DIR`` environment variable, which keeps tests and
throwaway instances from touching real data.
"""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FINLIFY_DATA_DIR", BASE_DIR)).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = DATA_DIR / "finlify.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(
    autoflush=False,
    expire_on_commit=False,
    bind=engine,
)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""
