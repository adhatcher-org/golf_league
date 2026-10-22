"""Flush-only historical snapshot creation and nine relabeling."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import Golfer, Season, Week, WeekHandicap
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


@dataclass(frozen=True)
class HandicapTrend:
    """A golfer's handicap trend across weeks."""

    golfer_id: int
    golfer_name: str
    weeks: Sequence["HandicapWeek"]


@dataclass(frozen=True)
class HandicapWeek:
    """Handicap data for one week."""

    week_id: int
    week_date: str
    week_index: int
    nine: str
    strokes: int | None
    source: str | None


def get_handicap_trend(session: Session, golfer_id: int, season_id: int | None = None) -> HandicapTrend | None:
    """Return a golfer's handicap trend for a season, or across all seasons.

    Returns None when the golfer does not exist. Missing handicaps are rendered
    as None (not zero) and never recomputed.
    """
    golfer = session.get(Golfer, golfer_id)
    if golfer is None:
        return None

    query = (select(WeekHandicap, Week)
             .join(Week)
             .where(WeekHandicap.golfer_id == golfer_id)
             .order_by(Week.play_date, Week.index))
    if season_id is not None:
        query = query.where(Week.season_id == season_id)

    rows = list(session.execute(query))
    weeks = []
    for handicap, week in rows:
        weeks.append(HandicapWeek(
            week_id=week.id,
            week_date=week.play_date.strftime("%b %-d, %Y"),
            week_index=week.index,
            nine=handicap.nine,
            strokes=handicap.strokes,
            source=handicap.source,
        ))

    return HandicapTrend(
        golfer_id=golfer.id,
        golfer_name=f"{golfer.first_name} {golfer.last_name}",
        weeks=tuple(weeks),
    )
