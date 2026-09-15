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
from sqlalchemy import update
from sqlalchemy.orm import Session

from golf_league.domain.tokens import digest, generate_token
from golf_league.models import UserToken

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


__all__ = [
    "hash_password",
    "verify_password",
    "create_session_cookie",
    "load_session_cookie",
    "issue_token",
    "consume_token",
]
