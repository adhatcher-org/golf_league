"""HTTP glue: CSRF helpers and the FastAPI dependency ladder.

This module owns no routes — GL-04 owns those. It only exposes the
dependencies routes will use to require an authenticated, verified, or
admin user, plus CSRF token generation/validation.
"""

import hashlib
import hmac

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from golf_league.config import Settings, get_settings
from golf_league.database import get_session
from golf_league.models import User
from golf_league.services.auth import load_session_cookie

SESSION_COOKIE_NAME = "session"

# A fixed, non-secret pepper. Security comes from the session value itself
# (an unguessable, server-signed cookie an attacker cannot read cross-site),
# not from this constant.
_CSRF_PEPPER = b"golf_league-csrf-token-v1"


def _current_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = get_settings()
    return settings


def generate_csrf_token(session_value: str) -> str:
    """Return a CSRF token bound to `session_value`.

    Deterministic for a given session value so it can be embedded once in a
    rendered form and re-checked on submit, without server-side storage.
    """
    if not session_value:
        raise ValueError("session_value is required to generate a CSRF token")
    return hmac.new(
        session_value.encode("utf-8"), _CSRF_PEPPER, hashlib.sha256
    ).hexdigest()


def validate_csrf(session_value: str, submitted: str) -> bool:
    """Return True only when `submitted` matches the token for `session_value`.

    Missing or empty inputs, and any mismatch, fail closed.
    """
    if not session_value or not submitted:
        return False
    expected = generate_csrf_token(session_value)
    return hmac.compare_digest(expected, submitted)


async def require_user(
    request: Request, session: Session = Depends(get_session)  # noqa: B008
) -> User:
    """Reject anonymous or invalid sessions; otherwise return the User.

    A session is invalid when the cookie is missing, its signature is
    tampered or malformed, the user id it names does not exist, or the
    session version it carries no longer matches the user's current one
    (i.e. every prior session was invalidated by a version bump).
    """
    settings = _current_settings(request)
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    loaded = load_session_cookie(cookie_value, settings.session_secret)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    user_id, session_version = loaded
    user = session.get(User, user_id)
    if user is None or user.session_version != session_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    return user


async def require_verified_user(user: User = Depends(require_user)) -> User:  # noqa: B008
    """Reject a valid but unverified session; `is_admin` is never a bypass."""
    if user.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Email verification required"
        )
    return user


async def require_admin(user: User = Depends(require_verified_user)) -> User:  # noqa: B008
    """Reject a verified non-admin user."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return user
