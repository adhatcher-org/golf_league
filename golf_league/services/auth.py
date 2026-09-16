"""Session-taking authentication primitives: passwords, session cookies, tokens.

This module may import sqlalchemy and golf_league.models, but never fastapi —
HTTP concerns (cookies on a Request/Response, CSRF, dependency ladders) live
in golf_league/security.py.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from golf_league.domain.identity import normalize_email
from golf_league.domain.tokens import digest, generate_token
from golf_league.models import User, UserToken

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password with Argon2 (never bcrypt, never plaintext)."""
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Return True when `plaintext` matches the Argon2 `stored_hash`.

    Any failure to verify (mismatch, malformed hash) is treated as "does not
    match" rather than propagating an exception.
    """
    try:
        return _hasher.verify(stored_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_cookie(user_id: int, session_version: int, secret: str) -> str:
    """Return a signed cookie value carrying `user_id` and `session_version`."""
    payload = f"{user_id}.{session_version}"
    signature = _sign(payload, secret)
    return f"{payload}.{signature}"


def load_session_cookie(value: str, secret: str) -> tuple[int, int] | None:
    """Verify a cookie's signature and return `(user_id, session_version)`.

    Returns None for a tampered signature, or for a value that is missing,
    empty, or not in the expected `uid.version.signature` shape. Checking
    the decoded user id and session version against the database (an
    unknown user id, or a session version that no longer matches the
    user's current one) is the caller's job — see
    `golf_league.security.require_user`.
    """
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    user_id_raw, version_raw, signature = parts
    payload = f"{user_id_raw}.{version_raw}"
    expected = _sign(payload, secret)
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        return int(user_id_raw), int(version_raw)
    except ValueError:
        return None


def issue_token(session: Session, user_id: int, purpose: str, ttl_seconds: int) -> str:
    """Issue a new single-use token for `user_id` and `purpose`.

    Any prior outstanding (unconsumed, unrevoked) token of the same purpose
    for this user is revoked first. Only the SHA-256 digest of the raw
    token is stored; the raw token is returned once and never persisted or
    logged.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    session.execute(
        update(UserToken)
        .where(
            UserToken.user_id == user_id,
            UserToken.purpose == purpose,
            UserToken.consumed_at.is_(None),
            UserToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )

    raw_token = generate_token()
    token_digest = digest(raw_token)
    expires_at = now + timedelta(seconds=ttl_seconds)

    token_row = UserToken(
        user_id=user_id,
        token_digest=token_digest,
        purpose=purpose,
        expires_at=expires_at,
    )
    session.add(token_row)
    session.commit()
    return raw_token


def consume_token(session: Session, raw_token: str, purpose: str) -> int | None:
    """Atomically consume a token, returning its `user_id`, or None.

    A single conditional UPDATE guards `consumed_at IS NULL`, `revoked_at
    IS NULL`, the exact `purpose`, and the (naive, timezone-free) expiry —
    all in one statement, so two concurrent callers racing for the same
    token can never both succeed: only the first UPDATE's WHERE clause
    still matches, so at most one row is ever touched.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    token_digest = digest(raw_token)

    stmt = (
        update(UserToken)
        .where(
            UserToken.token_digest == token_digest,
            UserToken.purpose == purpose,
            UserToken.consumed_at.is_(None),
            UserToken.revoked_at.is_(None),
            UserToken.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(UserToken.user_id)
    )
    result = session.execute(stmt)
    row = result.first()
    session.commit()
    if row is None:
        return None
    return row[0]


def peek_token(session: Session, raw_token: str, purpose: str) -> int | None:
    """Return the `user_id` of a still-valid, unconsumed token, without consuming it.

    Read-only counterpart to `consume_token`, used to decide whether a
    token-bound form (e.g. "set a new password") should render at all,
    before the user submits anything. Never mutates the row.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    token_digest = digest(raw_token)
    stmt = select(UserToken.user_id).where(
        UserToken.token_digest == token_digest,
        UserToken.purpose == purpose,
        UserToken.consumed_at.is_(None),
        UserToken.revoked_at.is_(None),
        UserToken.expires_at > now,
    )
    row = session.execute(stmt).first()
    return row[0] if row is not None else None


def get_user_by_email(session: Session, email: str) -> User | None:
    """Return the `User` matching a normalized email, or None."""
    normalized = normalize_email(email)
    return session.execute(
        select(User).where(User.email == normalized)
    ).scalar_one_or_none()


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    """Return the `User` matching `email` and `password`, or None.

    An unknown email and a wrong password are indistinguishable to the
    caller: both simply return None so a route can render one neutral
    failure without ever learning which case it was.
    """
    user = get_user_by_email(session, email)
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def mark_email_verified(session: Session, user_id: int, now: datetime) -> None:
    """Set `email_verified_at` on `user_id`, if that user still exists."""
    user = session.get(User, user_id)
    if user is None:
        return
    user.email_verified_at = now
    session.commit()


def complete_password_reset(session: Session, user_id: int, new_password: str) -> None:
    """Set a new password on `user_id` and bump `session_version`.

    Bumping `session_version` invalidates every session cookie issued
    before the reset, which is the entire point of that column.
    """
    user = session.get(User, user_id)
    if user is None:
        return
    user.password_hash = hash_password(new_password)
    user.session_version += 1
    session.commit()


__all__ = [
    "hash_password",
    "verify_password",
    "create_session_cookie",
    "load_session_cookie",
    "issue_token",
    "consume_token",
    "peek_token",
    "get_user_by_email",
    "authenticate_user",
    "mark_email_verified",
    "complete_password_reset",
]
