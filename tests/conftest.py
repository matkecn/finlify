"""Shared pytest fixtures for the Finlify test suite.

The application reads ``FINLIFY_DATA_DIR`` at import time, so a throwaway
directory is selected before any application module is imported. Each test then
runs against a freshly migrated, freshly seeded database.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["FINLIFY_DATA_DIR"] = tempfile.mkdtemp(prefix="finlify-tests-")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import migrations  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    """A TestClient bound to the application, with the app lifecycle managed."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_db():
    """Reset the database to a freshly migrated, seeded state before each test."""
    Base.metadata.drop_all(bind=engine)
    migrations.run(engine)
    yield


@pytest.fixture
def db():
    """A direct database session for arranging state or asserting stored rows."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
