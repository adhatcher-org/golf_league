"""Create and resolve player substitute requests without granting substitution rights."""

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.models import (
    MatchScoreState,
    PlayerMatch,
    SubstituteRequest,
    TeamMatch,
    User,
    Week,
)
from golf_league.services.matchups import reserve_matchup_write, substitute_player_core


class RequestNotFound(Exception):
    """The actor cannot request a substitute for this match."""


class RequestConflict(Exception):
    """The request or match cannot be changed in its current state."""


def _match_context(session: Session, week_id: int, player_match_id: int):
    match = session.get(PlayerMatch, player_match_id)
    pairing = session.get(TeamMatch, match.team_match_id) if match else None
    week = session.get(Week, week_id)
    if pairing is None or week is None or pairing.week_id != week_id:
        raise RequestNotFound
    return week, match


def _locked(session: Session, match_id: int) -> bool:
    state = session.get(MatchScoreState, match_id)
    return state is not None and state.locked_at is not None


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_request(session: Session, *, week_id: int, player_match_id: int,
                   golfer_id: int | None, clock: Callable[[], datetime]) -> tuple[SubstituteRequest, str, list[str]]:
    """Commit one request; return its raw token only to the email dispatcher."""
    if golfer_id is None:
        raise RequestNotFound
    try:
        reserve_matchup_write(session)
        week, match = _match_context(session, week_id, player_match_id)
        if golfer_id not in (match.a_golfer_id, match.b_golfer_id):
            raise RequestNotFound
        if week.status != "scheduled" or week.week_type != "match" or _locked(session, match.id):
            raise RequestConflict("This match cannot accept a substitute request.")
        if session.scalar(select(SubstituteRequest.id).where(SubstituteRequest.player_match_id == match.id)):
            raise RequestConflict("A substitute request already exists for this match.")
        recipients = list(session.scalars(select(User.email).where(
            User.is_admin.is_(True), User.email_verified_at.is_not(None)
        ).order_by(User.id)))
        if not recipients:
            raise RequestConflict("No verified league admin can receive the request.")
        raw_token = secrets.token_urlsafe(32)
        row = SubstituteRequest(week_id=week.id, player_match_id=match.id,
                                requesting_golfer_id=golfer_id, status="pending",
                                token_digest=_digest(raw_token), created_at=clock())
        session.add(row)
        session.commit()
        return row, raw_token, recipients
    except IntegrityError as exc:
        session.rollback()
        raise RequestConflict("A substitute request already exists for this match.") from exc
    except Exception:
        session.rollback()
        raise


def pending_requests(session: Session) -> list[SubstituteRequest]:
    return list(session.scalars(select(SubstituteRequest).where(
        SubstituteRequest.status == "pending"
    ).order_by(SubstituteRequest.created_at, SubstituteRequest.id)))


def respond(session: Session, *, request_id: int, raw_token: str, action: str,
            golfer_id: int | None, clock: Callable[[], datetime]) -> SubstituteRequest:
    """Consume the token and, on fulfillment, substitute in the same transaction."""
    try:
        reserve_matchup_write(session)
        row = session.get(SubstituteRequest, request_id)
        if (row is None or row.status != "pending" or not raw_token
                or not secrets.compare_digest(row.token_digest, _digest(raw_token))):
            raise RequestNotFound
        week, match = _match_context(session, row.week_id, row.player_match_id)
        if week.status != "scheduled" or week.week_type != "match" or _locked(session, match.id):
            raise RequestConflict("This match can no longer be changed.")
        if action == "fulfill":
            if golfer_id is None:
                raise RequestConflict("Choose an eligible substitute.")
            if match.a_golfer_id == row.requesting_golfer_id:
                side = "a"
            elif match.b_golfer_id == row.requesting_golfer_id:
                side = "b"
            else:
                raise RequestConflict("The requesting golfer no longer plays this match.")
            substitute_player_core(session, week.season_id, match.id,
                                   side=side, golfer_id=golfer_id, clock=clock)
            row.status = "fulfilled"
        elif action == "decline":
            row.status = "declined"
        else:
            raise RequestConflict("Choose fulfill or decline.")
        row.responded_at = clock()
        session.commit()
        return row
    except Exception:
        session.rollback()
        raise
