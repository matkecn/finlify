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

ADMIN_USERNAME = "owner"
ADMIN_PASSWORD = "owner-password-123"
SECOND_USERNAME = "sam"
SECOND_PASSWORD = "sam-password-123"


@pytest.fixture(scope="session")
def client() -> TestClient:
    """A TestClient bound to the application, with the app lifecycle managed."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_db(client: TestClient):
    """Reset the database, claim the owner account, and sign the client in.

    Every test starts from the same state a first launch produces: a migrated
    database with a single administrator that has just set a password, and a
    client holding that administrator's session.
    """
    Base.metadata.drop_all(bind=engine)
    migrations.run(engine)
    response = client.post(
        "/api/auth/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text
    yield


@pytest.fixture
def signed_out(client: TestClient) -> TestClient:
    """A client with no session cookie, for testing what anonymous callers see."""
    client.cookies.clear()
    yield client
    client.cookies.clear()


@pytest.fixture
def other_user(client: TestClient) -> dict:
    """Create a second local account and return it, with the owner still signed in.

    The new account's own session is discarded so tests start each isolation
    check from the administrator's point of view.
    """
    created = client.post(
        "/api/users",
        json={"username": SECOND_USERNAME, "password": SECOND_PASSWORD},
    )
    assert created.status_code == 201, created.text
    user = created.json()
    client.post("/api/auth/logout")
    response = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return user


@pytest.fixture
def sign_in_as(client: TestClient):
    """Return a helper that signs the client in as a username and password."""

    def _sign_in(username: str, password: str) -> None:
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text

    return _sign_in


@pytest.fixture
def db():
    """A direct database session for arranging state or asserting stored rows."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
