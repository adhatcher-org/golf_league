"""Pure token helpers: no I/O, no clock, no persistence.

`now` is always supplied by the caller. Nothing here ever calls
`time.time()`, `datetime.now()`, or `datetime.utcnow()` itself.
"""

import hashlib
import secrets
from datetime import datetime


def generate_token() -> str:
    """Return a new random URL-safe token (32 bytes of entropy)."""
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    """Return the SHA-256 hex digest of `token`.

    Only this digest is ever persisted; the raw token is returned to the
    caller once and never stored or logged.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_expired(expires_at: datetime, now: datetime) -> bool:
    """Return True when `now` is at or past `expires_at`.

    `now` is always injected by the caller — this function never reads the
    clock itself, which is what makes it testable without `sleep`.
    """
    return now >= expires_at
