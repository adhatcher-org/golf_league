"""Admin bootstrap configuration and logic.

`Settings` (golf_league/config.py) has no ADMIN_EMAIL/ADMIN_PASSWORD fields
and this task must not add any (GL-02's and GL-60's Must-not lists forbid
editing config.py). Those two values only ever matter once, at first boot,
so they are read directly from the environment here instead.
"""

import logging
import os
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.identity import normalize_email
from golf_league.models import User
from golf_league.services.auth import hash_password

logger = logging.getLogger(__name__)


def bootstrap_admin(session: Session) -> None:
    """Create the first admin user if, and only if, `users` is empty.

    Reads `ADMIN_EMAIL` and `ADMIN_PASSWORD` from the environment. The
    admin is created pre-verified (`email_verified_at` set) because SMTP
    does not exist until M7 and requiring verification here would make
    login impossible.

    Two workers may both see zero users and both attempt this insert. The
    unique constraint on `users.email` (not a check-then-insert) is what
    makes that race safe: the loser's commit raises `IntegrityError`, which
    is caught here so the caller sees no unhandled exception and no
    duplicate admin.
    """
    count = session.execute(select(func.count()).select_from(User)).scalar_one()
    if count != 0:
        return

    admin_email = os.environ.get("ADMIN_EMAIL")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if not admin_email or not admin_password:
        logger.warning(
            "admin bootstrap skipped: set both ADMIN_EMAIL and ADMIN_PASSWORD "
            "in the environment to create the first admin account"
        )
        return

    normalized_email = normalize_email(admin_email)
    now = datetime.now(UTC).replace(tzinfo=None)
    admin = User(
        username=normalized_email,
        email=normalized_email,
        display_name="Admin",
        password_hash=hash_password(admin_password),
        email_verified_at=now,
        is_admin=True,
    )
    session.add(admin)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.info(
            "admin bootstrap skipped: a concurrent boot already created the admin"
        )
