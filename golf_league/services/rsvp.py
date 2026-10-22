"""Weekly attendance responses and the admin's season-scoped headcount."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import (
    Golfer,
    PlayerMatch,
    SeasonGolfer,
    TeamMatch,
    Week,
    WeekRsvp,
)

STATUSES = frozenset({"playing", "not_playing", "maybe"})


class RsvpError(ValueError):
    """An invalid attendance response or unavailable week."""


@dataclass(frozen=True)
class RsvpRow:
    golfer_id: int
    name: str
    status: str | None
    email: str | None


@dataclass(frozen=True)
class RsvpSummary:
    rows: list[RsvpRow]
    playing: int
    not_playing: int
    maybe: int
    no_response: int


def get_rsvp(session: Session, week_id: int, golfer_id: int) -> WeekRsvp | None:
    return session.scalar(select(WeekRsvp).where(
        WeekRsvp.week_id == week_id, WeekRsvp.golfer_id == golfer_id,
    ))


def set_rsvp(session: Session, week_id: int, golfer_id: int, status: str) -> WeekRsvp:
    """Update the caller's linked golfer; caller identity is decided by the HTTP layer."""
    if status not in STATUSES:
        raise RsvpError("Choose playing, not playing, or maybe.")
    if session.get(Week, week_id) is None or session.get(Golfer, golfer_id) is None:
        raise RsvpError("Week or golfer not found.")
    row = get_rsvp(session, week_id, golfer_id)
    if row is None:
        row = WeekRsvp(week_id=week_id, golfer_id=golfer_id, status=status)
        session.add(row)
    else:
        row.status = status
    session.commit()
    return row


def week_summary(session: Session, week_id: int) -> RsvpSummary:
    """Include active season roster golfers and everyone in a generated match."""
    week = session.get(Week, week_id)
    if week is None:
        raise RsvpError("Week not found.")
    roster_ids = set(session.scalars(
        select(SeasonGolfer.golfer_id).join(Golfer, Golfer.id == SeasonGolfer.golfer_id).where(
            SeasonGolfer.season_id == week.season_id, Golfer.is_active.is_(True),
        )
    ))
    match_ids = session.execute(
        select(PlayerMatch.a_golfer_id, PlayerMatch.b_golfer_id)
        .join(TeamMatch, PlayerMatch.team_match_id == TeamMatch.id)
        .where(TeamMatch.week_id == week_id)
    ).all()
    ids = roster_ids | {golfer_id for sides in match_ids for golfer_id in sides}
    golfers = session.scalars(select(Golfer).where(Golfer.id.in_(ids)).order_by(
        Golfer.last_name, Golfer.first_name, Golfer.id,
    )).all() if ids else []
    statuses = {row.golfer_id: row.status for row in session.scalars(
        select(WeekRsvp).where(WeekRsvp.week_id == week_id)
    )}
    rows = [RsvpRow(golfer.id, f"{golfer.first_name} {golfer.last_name}",
                    statuses.get(golfer.id), golfer.email) for golfer in golfers]
    return RsvpSummary(rows, *(sum(row.status == status for row in rows)
                               for status in ("playing", "not_playing", "maybe", None)))


def reminder_recipients(summary: RsvpSummary) -> list[str]:
    """One message per distinct address, for golfers who have not responded."""
    return list(dict.fromkeys(row.email for row in summary.rows if row.status is None and row.email))
