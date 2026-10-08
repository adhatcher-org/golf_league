"""Invitation policy and write primitives; owners serialize and commit operations.

Admission requires BEGIN IMMEDIATE before any database reads. These helpers only
flush: callers commit admission before dispatch, and use fresh serialized
transactions for callbacks. Raw delivery records must never be logged.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from golf_league.domain.identity import is_valid_email, normalize_email
from golf_league.domain.rate_limit import check_window
from golf_league.domain.tokens import digest, generate_token
from golf_league.models import (
    Golfer,
    GolferSetPasswordToken,
    InviteRateWindow,
    LeagueInviteLink,
    User,
    UserToken,
)

QUOTAS = {"email": (3, 900), "ip": (10, 3600), "invite": (40, 3600), "global": (100, 3600)}


@dataclass(frozen=True)
class CreatedInvite:
    invite_id: int
    raw_token: str = field(repr=False)


@dataclass(frozen=True)
class InviteDelivery:
    token_id: int
    invite_id: int
    recipient_email: str = field(repr=False)
    raw_token: str = field(repr=False)


@dataclass(frozen=True)
class SetPasswordSubject:
    token_id: int
    golfer_id: int
    invite_id: int
    email_snapshot: str = field(repr=False)
    expected_user_id: int | None


@dataclass(frozen=True)
class CompletedCredential:
    user_id: int
    session_version: int


class AccountCapacityReached(ValueError):
    """A new account was refused because the configured account cap is full."""


class InviteValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("Invalid invitation fields")
        self.errors = errors


class InviteNotFound(LookupError):
    """The referenced invitation does not exist."""


class InviteConflict(ValueError):
    """A revoked or expired invitation cannot be rotated."""


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def _live(invite: LeagueInviteLink, now: datetime) -> bool:
    return invite.revoked_at is None and invite.expires_at > now


def create_invite(
    session: Session, *, created_by_user_id: int, label: str,
    expiry_days: str = "", now: datetime,
) -> CreatedInvite:
    now = _utc(now)
    errors = {}
    label = label.strip()
    if not label or len(label) > 120:
        errors["label"] = "Enter a label of 1 to 120 characters."
    days_text = expiry_days.strip() or "30"
    expires_at = None
    if days_text.isdecimal() and len(days_text) <= 7:
        days = int(days_text)
        if days > 0:
            try:
                expires_at = now + timedelta(days=days)
            except OverflowError:
                expires_at = None
    if expires_at is None:
        errors["expiry_days"] = "Enter a positive number of days within the supported date range."
    if errors:
        raise InviteValidationError(errors)
    raw = generate_token()
    invite = LeagueInviteLink(
        token_hash=digest(raw), label=label, created_by_user_id=created_by_user_id,
        created_at=now, expires_at=expires_at,
    )
    session.add(invite)
    session.flush()
    return CreatedInvite(invite.id, raw)


def revoke_invite(session: Session, *, invite_id: int, now: datetime) -> bool:
    now = _utc(now)
    invite = session.get(LeagueInviteLink, invite_id)
    if invite is None:
        return False
    if invite.revoked_at is None:
        invite.revoked_at = now
    session.execute(
        update(GolferSetPasswordToken).where(
            GolferSetPasswordToken.invite_link_id == invite_id,
            GolferSetPasswordToken.consumed_at.is_(None),
            GolferSetPasswordToken.revoked_at.is_(None),
        ).values(revoked_at=now)
    )
    session.flush()
    return True


def rotate_invite(
    session: Session, *, invite_id: int, created_by_user_id: int, now: datetime,
) -> CreatedInvite:
    now = _utc(now)
    invite = session.get(LeagueInviteLink, invite_id)
    if invite is None:
        raise InviteNotFound()
    if not _live(invite, now):
        raise InviteConflict()
    label = invite.label
    revoke_invite(session, invite_id=invite_id, now=now)
    return create_invite(session, created_by_user_id=created_by_user_id, label=label, now=now)


def get_valid_invite(session: Session, *, raw_token: str, now: datetime) -> LeagueInviteLink | None:
    now = _utc(now)
    return session.scalar(select(LeagueInviteLink).where(
        LeagueInviteLink.token_hash == digest(raw_token),
        LeagueInviteLink.revoked_at.is_(None), LeagueInviteLink.expires_at > now,
    ))


def _matching_identity(session: Session, golfer: Golfer, email: str) -> tuple[bool, int | None]:
    """Reject both collisions and linkage drift rather than repair identity."""
    users = session.scalars(select(User).where(or_(
        func.lower(func.trim(User.email)) == email, User.golfer_id == golfer.id,
    ))).all()
    if not users:
        return True, None
    if len(users) != 1:
        return False, None
    user = users[0]
    if user.golfer_id != golfer.id or normalize_email(user.email) != email:
        return False, None
    return True, user.id


def _key(secret: str, tier: str, value: str) -> str:
    return hmac.new(secret.encode(), f"league-invite:{tier}:{value}".encode(), hashlib.sha256).hexdigest()


def admit_join_send(
    session: Session, *, raw_invite_token: str, email: str, client_ip: str,
    secret: str, now: datetime,
) -> InviteDelivery | None:
    """Reserve all tiers and issue once inside the owner's immediate transaction."""
    now = _utc(now)
    invite = get_valid_invite(session, raw_token=raw_invite_token, now=now)
    if invite is None:
        return None
    email = normalize_email(email)
    epoch = now.timestamp()
    values = {"email": email, "ip": client_ip, "invite": str(invite.id), "global": "all"}
    prospective = []
    allowed = True
    for tier, value in values.items():
        key = _key(secret, tier, value)
        row = session.get(InviteRateWindow, (tier, key))
        state = (row.window_start, row.count) if row else None
        limit, seconds = QUOTAS[tier]
        accepted, next_state = check_window(state, now=epoch, limit=limit, window_seconds=seconds)
        allowed = allowed and accepted
        prospective.append((tier, key, next_state))
    if not allowed:
        session.execute(update(LeagueInviteLink).where(LeagueInviteLink.id == invite.id).values(
            suppressed_count=LeagueInviteLink.suppressed_count + 1,
        ))
        session.flush()
        return None
    if invite.require_phone_last_four or len(email) > 254 or not is_valid_email(email):
        return None
    golfer = session.scalar(select(Golfer).where(
        Golfer.email == email, Golfer.is_active.is_(True),
    ))
    if golfer is None:
        return None
    matches, user_id = _matching_identity(session, golfer, email)
    if not matches or (user_id is None and (session.scalar(select(func.count()).select_from(User)) or 0) >= 150):
        return None

    # Prune obsolete windows only after admission; active windows never change
    # on an ineligible or suppressed request.
    session.execute(delete(InviteRateWindow).where(or_(*[
        (InviteRateWindow.tier == tier) & (InviteRateWindow.window_start <= epoch - seconds)
        for tier, (_, seconds) in QUOTAS.items()
    ])))
    for tier, key, (start, count) in prospective:
        statement = insert(InviteRateWindow).values(
            tier=tier, key_digest=key, window_start=start, count=count,
        )
        session.execute(statement.on_conflict_do_update(
            index_elements=["tier", "key_digest"],
            set_={"window_start": start, "count": count},
        ))
    session.execute(update(GolferSetPasswordToken).where(
        GolferSetPasswordToken.golfer_id == golfer.id,
        GolferSetPasswordToken.consumed_at.is_(None),
        GolferSetPasswordToken.revoked_at.is_(None),
    ).values(revoked_at=now))
    raw = generate_token()
    token = GolferSetPasswordToken(
        golfer_id=golfer.id, invite_link_id=invite.id, email_snapshot=email,
        expected_user_id=user_id, token_digest=digest(raw),
        expires_at=now + timedelta(minutes=45), created_at=now,
    )
    session.add(token)
    session.flush()
    return InviteDelivery(token.id, invite.id, email, raw)


def peek_set_password_token(session: Session, *, raw_token: str, now: datetime) -> SetPasswordSubject | None:
    now = _utc(now)
    token = session.scalar(select(GolferSetPasswordToken).where(
        GolferSetPasswordToken.token_digest == digest(raw_token),
        GolferSetPasswordToken.expires_at > now,
        GolferSetPasswordToken.revoked_at.is_(None), GolferSetPasswordToken.consumed_at.is_(None),
    ))
    if token is None:
        return None
    invite = session.get(LeagueInviteLink, token.invite_link_id)
    golfer = session.get(Golfer, token.golfer_id)
    if (
        invite is None or not _live(invite, now) or invite.require_phone_last_four
        or golfer is None or not golfer.is_active or golfer.email is None
        or normalize_email(golfer.email) != token.email_snapshot
    ):
        return None
    matches, user_id = _matching_identity(session, golfer, token.email_snapshot)
    if not matches or user_id != token.expected_user_id:
        return None
    return SetPasswordSubject(token.id, token.golfer_id, token.invite_link_id, token.email_snapshot, user_id)


def complete_set_password(
    session: Session, *, raw_token: str, password_hash: str, now: datetime,
) -> CompletedCredential | None:
    """Complete a roster credential atomically; caller owns BEGIN IMMEDIATE/commit."""
    now = _utc(now)
    subject = peek_set_password_token(session, raw_token=raw_token, now=now)
    if subject is None:
        return None

    # Re-read the roster row after acquiring the caller's write reservation.
    golfer = session.get(Golfer, subject.golfer_id)
    if golfer is None:
        return None
    user = session.get(User, subject.expected_user_id) if subject.expected_user_id is not None else None
    if subject.expected_user_id is not None and user is None:
        return None
    if subject.expected_user_id is None:
        if (session.scalar(select(func.count()).select_from(User)) or 0) >= 150:
            raise AccountCapacityReached()
        user = User(
            username=subject.email_snapshot,
            email=subject.email_snapshot,
            display_name=f"{golfer.first_name} {golfer.last_name}",
            password_hash=password_hash,
            email_verified_at=now.replace(tzinfo=None),
            is_admin=False,
            golfer_id=golfer.id,
            session_version=1,
        )
        session.add(user)
        session.flush()
    else:
        user.password_hash = password_hash
        user.email_verified_at = now.replace(tzinfo=None)
        user.session_version += 1

    token_result = session.execute(
        update(GolferSetPasswordToken)
        .where(
            GolferSetPasswordToken.token_digest == digest(raw_token),
            GolferSetPasswordToken.expires_at > now,
            GolferSetPasswordToken.consumed_at.is_(None),
            GolferSetPasswordToken.revoked_at.is_(None),
        )
        .values(consumed_at=now)
        .returning(GolferSetPasswordToken.id)
    ).first()
    if token_result is None:
        return None

    session.execute(update(GolferSetPasswordToken).where(
        GolferSetPasswordToken.golfer_id == golfer.id,
        GolferSetPasswordToken.consumed_at.is_(None),
        GolferSetPasswordToken.revoked_at.is_(None),
    ).values(revoked_at=now))
    session.execute(update(UserToken).where(
        UserToken.user_id == user.id,
        UserToken.purpose.in_(("reset_password", "set_password", "verify_email")),
        UserToken.consumed_at.is_(None), UserToken.revoked_at.is_(None),
    ).values(revoked_at=now.replace(tzinfo=None)))
    session.flush()
    return CompletedCredential(user.id, user.session_version)


def record_send_success(session: Session, *, token_id: int, now: datetime) -> None:
    now = _utc(now)
    invite_id = select(GolferSetPasswordToken.invite_link_id).where(GolferSetPasswordToken.id == token_id).scalar_subquery()
    session.execute(update(LeagueInviteLink).where(LeagueInviteLink.id == invite_id).values(
        send_count=LeagueInviteLink.send_count + 1, last_sent_at=now,
    ))
    session.flush()


def record_send_failure(session: Session, *, token_id: int, now: datetime) -> None:
    now = _utc(now)
    session.execute(update(GolferSetPasswordToken).where(
        GolferSetPasswordToken.id == token_id, GolferSetPasswordToken.revoked_at.is_(None),
    ).values(revoked_at=now))
    invite_id = select(GolferSetPasswordToken.invite_link_id).where(GolferSetPasswordToken.id == token_id).scalar_subquery()
    session.execute(update(LeagueInviteLink).where(LeagueInviteLink.id == invite_id).values(
        failed_send_count=LeagueInviteLink.failed_send_count + 1,
    ))
    session.flush()
