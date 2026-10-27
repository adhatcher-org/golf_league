"""Flush-only historical snapshot creation and nine relabeling."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.reseed import (
    ReseedCandidate,
    ReseedPlan,
    reseed_plan,
)
from golf_league.domain.scorecard import Actor
from golf_league.models import (
    Golfer,
    PlayerMatch,
    Season,
    TeamMatch,
    Week,
    WeekHandicap,
)
from golf_league.services.matchups import (
    MatchupConflict,
    MatchupValidationError,
    _effective,
    _flush,
    _generated_time,
    reserve_matchup_write,
)
from golf_league.services.participants import (
    ParticipantValidationError,
    effective_participant,
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


class HandicapReseedError(Exception):
    """Re-seed failure with a machine-readable code and a human message."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ReseedPreview:
    """Read-only re-seed preview for a week."""

    week_id: int
    week_index: int
    week_date: str
    nine: str
    season_id: int
    plan: ReseedPlan


def reseed_preview(session: Session, week_id: int) -> ReseedPreview:
    """Read-only preview of what re-seeding this week would change.

    Never mutates state. Categorises every golfer with a match slot in the
    week into changes/unchanged/blocked/excluded using `reseed_plan`.
    """
    week = session.get(Week, week_id)
    if week is None:
        raise HandicapReseedError("week_not_found", "Week not found.")
    if week.status != "scheduled" or week.week_type != "match" or week.nine is None:
        raise HandicapReseedError("not_schedulable", "Choose a scheduled match week.")

    season = session.get(Season, week.season_id)
    if season is None:
        raise HandicapReseedError("season_not_found", "Season not found.")

    # Collect every golfer with a match slot in this week, tracking whether any
    # slot is manually adjusted.
    golfer_manually_adjusted: dict[int, bool] = {}
    team_matches = list(session.execute(
        select(TeamMatch.id).where(TeamMatch.week_id == week_id)
    ).scalars())
    if team_matches:
        slots = list(session.execute(
            select(PlayerMatch.a_golfer_id, PlayerMatch.b_golfer_id, PlayerMatch.manually_adjusted)
            .where(PlayerMatch.team_match_id.in_(team_matches))
        ).all())
        for a_id, b_id, ma in slots:
            golfer_manually_adjusted.setdefault(a_id, False)
            golfer_manually_adjusted.setdefault(b_id, False)
            if ma:
                golfer_manually_adjusted[a_id] = True
                golfer_manually_adjusted[b_id] = True

    candidates: list[ReseedCandidate] = []
    for golfer_id in sorted(golfer_manually_adjusted):
        golfer = session.get(Golfer, golfer_id)
        if golfer is None:
            continue
        manually_adjusted = golfer_manually_adjusted[golfer_id]

        # Existing WeekHandicap row (if any).
        row = session.scalar(select(WeekHandicap).where(
            WeekHandicap.week_id == week_id, WeekHandicap.golfer_id == golfer_id,
        ))
        current_strokes = row.strokes if row is not None else None

        # Effective tee + handicap for this golfer in this season.
        try:
            view = effective_participant(session, season.id, golfer_id)
        except ParticipantValidationError:
            view = None
        effective_handicap = view.effective_handicap if view is not None else None

        candidates.append(ReseedCandidate(
            golfer_id=golfer_id,
            golfer_name=f"{golfer.first_name} {golfer.last_name}",
            current_strokes=current_strokes,
            effective_handicap=effective_handicap,
            manually_adjusted=manually_adjusted,
        ))

    plan = reseed_plan(candidates)
    return ReseedPreview(
        week_id=week.id,
        week_index=week.index,
        week_date=week.play_date.strftime("%b %-d, %Y"),
        nine=week.nine,
        season_id=season.id,
        plan=plan,
    )


def reseed_week_handicaps(session: Session, week_id: int, actor: Actor, now: datetime) -> int:
    """Atomically overwrite will-change WeekHandicap rows for this week.

    Mutates existing rows only: strokes, tee_set_id, nine, source, computed_at.
    Never touches `manually_adjusted` match slots, never creates new rows.
    Caller owns `session.commit()`.

    Returns the number of rows overwritten.
    """
    reserve_matchup_write(session)
    preview = reseed_preview(session, week_id)
    week = session.get(Week, week_id)
    if week is None:
        raise HandicapReseedError("week_not_found", "Week not found.")

    count = 0
    for change in preview.plan.changes:
        row = session.scalar(select(WeekHandicap).where(
            WeekHandicap.week_id == week_id, WeekHandicap.golfer_id == change.golfer_id,
        ))
        if row is None:
            continue
        view = effective_participant(session, preview.season_id, change.golfer_id)
        row.strokes = change.new_strokes
        row.tee_set_id = view.effective_tee.id
        row.nine = week.nine
        row.source = "seeded"
        row.computed_at = now
        session.add(row)
        count += 1

    _flush(session)
    return count
