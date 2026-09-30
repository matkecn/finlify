"""Password hashing, session tokens, and sign-in sessions.

Everything here uses the standard library only, matching the project's promise
of zero third-party runtime dependencies. Passwords are stretched with
PBKDF2-HMAC-SHA256, sessions are random tokens whose SHA-256 digest is what
lands in the database, and every comparison is constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from sqlalchemy.orm import Session

COOKIE_NAME = "finlify_session"

SESSION_DAYS = 30

PBKDF2_ITERATIONS = 240_000

SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 8

MAX_PASSWORD_LENGTH = 200

_DECOY_HASH = f"pbkdf2_sha256${PBKDF2_ITERATIONS}$" + "00" * 16 + "00" * 32


class AuthError(Exception):
    """Raised when credentials cannot be honoured."""


def now() -> int:
    """Return the current unix timestamp in whole seconds."""
    return int(time.time())


def validate_password(password: str) -> str:
    """Return the password unchanged after checking it is usable.

    Raises:
        AuthError: If the password is too short, too long, or blank.
    """
    text = str(password or "")
    if not text:
        raise AuthError("Password is required")
    if len(text) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(text) > MAX_PASSWORD_LENGTH:
        raise AuthError("Password is too long")
    return text


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a self-describing PBKDF2 hash string for ``password``.

    The stored format is ``pbkdf2_sha256$iterations$salt_hex$hash_hex`` so the
    work factor can be raised later without invalidating existing passwords.
    """
    text = validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", text.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Return whether ``password`` matches the stored hash.

    A missing or malformed hash always returns ``False`` instead of raising, so
    a user without a password can never be signed into.
    """
    if not stored or not password:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str | None) -> bool:
    """Whether a stored hash should be upgraded to the current work factor."""
    if not stored:
        return False
    try:
        algorithm, iterations, _salt, _digest = stored.split("$")
    except ValueError:
        return False
    return algorithm != "pbkdf2_sha256" or int(iterations) < PBKDF2_ITERATIONS


def new_token() -> str:
    """Return a fresh, unguessable session token."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """Return the hex digest stored in place of a session token."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def expiry_timestamp(days: int = SESSION_DAYS) -> int:
    """Return the unix timestamp at which a new session should expire."""
    return now() + int(days) * 86400


def purge_expired(db: Session) -> int:
    """Delete sessions that have expired, returning how many were removed."""
    from models import AuthSession

    stale = db.query(AuthSession).filter(AuthSession.expires_at <= now()).all()
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
    return len(stale)


def create_session(db: Session, user) -> str:
    """Store a new session for ``user`` and return the raw token.

    Only the token digest is persisted, so the database never holds a value that
    could be replayed as a cookie.
    """
    from models import AuthSession

    token = new_token()
    db.add(AuthSession(
        token_hash=token_digest(token),
        user_id=user.id,
        created_at=now(),
        expires_at=expiry_timestamp(),
    ))
    db.commit()
    return token


def delete_session(db: Session, token: str | None) -> int:
    """Remove one session by raw token, returning how many rows were removed."""
    from models import AuthSession

    if not token:
        return 0
    row = db.get(AuthSession, token_digest(token))
    if row is None:
        return 0
    db.delete(row)
    db.commit()
    return 1


def delete_user_sessions(db: Session, user_id: int) -> int:
    """Remove every session belonging to a user, e.g. when a password changes."""
    from models import AuthSession

    rows = db.query(AuthSession).filter(AuthSession.user_id == user_id).all()
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def resolve_session(db: Session, token: str | None):
    """Return the :class:`~models.User` for a cookie token, or ``None``.

    An expired session is treated as absent and is cleaned up as a side effect.
    """
    from models import AuthSession, User

    if not token:
        return None
    row = db.get(AuthSession, token_digest(token))
    if row is None:
        return None
    if row.expires_at <= now():
        db.delete(row)
        db.commit()
        return None
    return db.get(User, row.user_id)


def authenticate(db: Session, username: str, password: str):
    """Return the user matching the credentials, or ``None``.

    The same generic failure is returned for an unknown username and a wrong
    password, so the endpoint cannot be used to enumerate accounts.
    """
    from models import User

    name = str(username or "").strip().lower()
    if not name:
        return None
    user = db.query(User).filter(User.username == name).first()
    stored = user.password_hash if user is not None else _DECOY_HASH
    if not verify_password(password, stored) or user is None:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    return user
