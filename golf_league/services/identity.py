"""Session-taking registration policy; HTTP and email delivery stay in routers."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.models import Golfer, User
from golf_league.services.auth import issue_token

VERIFY_TOKEN_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RegistrationResult:
    """A committed token delivery request, if registration was eligible."""

    email: str | None = None
    token: str | None = None


def register_roster_user(
    session: Session,
    *,
    email: str,
    password_hash: str,
    max_users: int,
    email_verification_required: bool = True,
) -> RegistrationResult:
    """Atomically create or retry a roster-linked unverified account.

    Every refusal is represented by an empty result.  SQLite's immediate
    transaction serializes the count, eligibility and unique-link checks.
    """
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        golfer = session.execute(
            select(Golfer).where(Golfer.email == email, Golfer.is_active.is_(True))
        ).scalar_one_or_none()
        existing_email = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()

        if existing_email is not None:
            if (
                golfer is not None
                and existing_email.golfer_id == golfer.id
                and existing_email.email_verified_at is None
            ):
                if not email_verification_required:
                    existing_email.email_verified_at = datetime.now(UTC).replace(tzinfo=None)
                    session.commit()
                    return RegistrationResult()
                token = issue_token(
                    session,
                    existing_email.id,
                    "verify_email",
                    VERIFY_TOKEN_TTL_SECONDS,
                    commit=False,
                )
                session.commit()
                return RegistrationResult(email=email, token=token)
            session.rollback()
            return RegistrationResult()

        if golfer is None:
            session.rollback()
            return RegistrationResult()

        linked_user = session.execute(
            select(User.id).where(User.golfer_id == golfer.id)
        ).first()
        user_count = session.scalar(select(func.count()).select_from(User)) or 0
        if linked_user is not None or user_count >= max_users:
            session.rollback()
            return RegistrationResult()

        user = User(
            username=email,
            email=email,
            display_name=f"{golfer.first_name} {golfer.last_name}",
            password_hash=password_hash,
            email_verified_at=(
                None
                if email_verification_required
                else datetime.now(UTC).replace(tzinfo=None)
            ),
            is_admin=False,
            golfer_id=golfer.id,
        )
        session.add(user)
        session.flush()
        token = None
        if email_verification_required:
            token = issue_token(
                session,
                user.id,
                "verify_email",
                VERIFY_TOKEN_TTL_SECONDS,
                commit=False,
            )
        session.commit()
        return RegistrationResult(email=email if token is not None else None, token=token)
    except IntegrityError:
        session.rollback()
        return RegistrationResult()
    except Exception:
        session.rollback()
        raise
