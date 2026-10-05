"""Flush-only historical snapshot creation and nine relabeling."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import Season, Week, WeekHandicap
from golf_league.services.matchups import (
    MatchupConflict,
    MatchupValidationError,
    _effective,
    _flush,
    _generated_time,
    reserve_matchup_write,
)


def ensure_week_handicap(session: Session, week_id: int, golfer_id: int, *, clock) -> WeekHandicap:
    reserve_matchup_write(session)
    week = session.get(Week, week_id)
    if week is None:
        raise LookupError("Week not found.")
    if week.status != "scheduled" or week.week_type != "match" or week.nine is None:
        raise MatchupValidationError({"week": "Choose a scheduled match week."})
    season = session.get(Season, week.season_id)
    view = _effective(session, season, golfer_id)
    existing = session.scalar(select(WeekHandicap).where(WeekHandicap.week_id == week_id, WeekHandicap.golfer_id == golfer_id))
    if existing is not None:
        if existing.tee_set_id != view.effective_tee.id or existing.nine != week.nine:
            raise MatchupConflict("The existing weekly snapshot has a conflicting tee or nine.")
        return existing
    prior = session.scalar(select(WeekHandicap.id).join(Week).where(
        Week.season_id == week.season_id, WeekHandicap.golfer_id == golfer_id,
    ).limit(1))
    row = WeekHandicap(week_id=week_id, golfer_id=golfer_id, tee_set_id=view.effective_tee.id,
                       nine=week.nine, strokes=view.effective_handicap,
                       source="carried" if prior is not None else "seeded", computed_at=_generated_time(clock))
    session.add(row)
    _flush(session)
    return row


def relabel_week_nine(session: Session, week_id: int, new_nine: str) -> None:
    """The caller already owns the serialized nine confirmation transaction."""
    if new_nine not in {"front", "back"}:
        raise MatchupValidationError({"new_nine": "Choose front or back nine."})
    for snapshot in session.scalars(select(WeekHandicap).where(WeekHandicap.week_id == week_id)):
        snapshot.nine = new_nine
    _flush(session)
