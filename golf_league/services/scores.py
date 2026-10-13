"""Scorecard entry: which weeks accept a card, who is on it, and the all-or-nothing write.

`submit_card` runs on the caller's session, flushes and never commits or rolls
back the caller's transaction (R-SCORES, R-SCHEDULE cross-side concurrency).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.scorecard import group_by_match, is_blank, validate_strokes
from golf_league.models import (
    Golfer,
    Hole,
    HoleScore,
    PlayerMatch,
    Season,
    TeamMatch,
    Week,
)
from golf_league.services.matchups import Clock, MatchupConflict, reserve_matchup_write
from golf_league.services.schedule import ScheduleConflict

ERROR_CODES = (
    "not_found", "past_week", "cancelled", "played", "no_matchup", "conflict",
    "golfer_count", "golfer_not_in_week", "missing_score", "bad_score",
    "needs_acknowledgement", "already_submitted",
)


class ScoreEntryError(Exception):
    """A refused scorecard operation with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Candidate:
    player_match_id: int
    golfer_id: int
    name: str
    opponent_golfer_id: int
    opponent_name: str


@dataclass(frozen=True)
class CardResult:
    week_id: int
    golfer_ids: tuple[int, ...]
    player_match_ids: tuple[int, ...]
    rows_written: int
    incomplete: bool


@dataclass(frozen=True)
class GolferScores:
    golfer_id: int
    name: str
    strokes_by_hole: dict[int, int]
    total: int


@dataclass(frozen=True)
class MatchScores:
    player_match_id: int
    position_label: str
    golfers: tuple[GolferScores, ...]


def league_today(clock: Clock, timezone_name: str) -> date:
    """League-local calendar date for the injected clock's aware instant."""
    return clock().astimezone(ZoneInfo(timezone_name)).date()


def _week_player_matches(session: Session, week_id: int) -> list[PlayerMatch]:
    return list(session.scalars(
        select(PlayerMatch)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id)
        .order_by(PlayerMatch.id)
    ))


def _has_matches(session: Session, week_id: int) -> bool:
    return session.scalar(
        select(PlayerMatch.id)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id)
        .limit(1)
    ) is not None


def eligible_weeks(session: Session, season_id: int, today: date) -> list[Week]:
    """Scheduled weeks dated today or later with generated matches, by date then index."""
    weeks = session.scalars(
        select(Week)
        .where(Week.season_id == season_id, Week.status == "scheduled", Week.play_date >= today)
        .order_by(Week.play_date, Week.index)
    )
    return [week for week in weeks if _has_matches(session, week.id)]


def default_week(session: Session, season_id: int, today: date) -> Week | None:
    """The eligible week dated today, else the next eligible week, else None."""
    weeks = eligible_weeks(session, season_id, today)
    for week in weeks:
        if week.play_date == today:
            return week
    return weeks[0] if weeks else None


def week_for_entry(session: Session, week_id: int, today: date) -> Week:
    """Return the week or raise ScoreEntryError; performs reads only."""
    week = session.get(Week, week_id)
    if week is None:
        raise ScoreEntryError("not_found", "That week does not exist.")
    if week.status == "cancelled":
        raise ScoreEntryError("cancelled", "That week is cancelled.")
    if week.status == "played":
        raise ScoreEntryError("played", "That week is already played.")
    if week.play_date < today:
        raise ScoreEntryError("past_week", "That week is in the past.")
    if not _has_matches(session, week.id):
        raise ScoreEntryError("no_matchup", "That week has no generated matches.")
    return week


def _name(golfer: Golfer) -> str:
    return f"{golfer.first_name} {golfer.last_name}".strip()


def card_candidates(session: Session, week_id: int) -> list[Candidate]:
    """Every golfer who is a side of a generated player match in the week."""
    result: list[Candidate] = []
    for match in _week_player_matches(session, week_id):
        a, b = session.get(Golfer, match.a_golfer_id), session.get(Golfer, match.b_golfer_id)
        result.append(Candidate(match.id, a.id, _name(a), b.id, _name(b)))
        result.append(Candidate(match.id, b.id, _name(b), a.id, _name(a)))
    return result


def week_holes(session: Session, week_id: int) -> list[Hole]:
    """The season course's holes on the week's nine, ordered by number."""
    week = session.get(Week, week_id)
    if week is None or week.nine is None:
        return []
    season = session.get(Season, week.season_id)
    return list(session.scalars(
        select(Hole)
        .where(Hole.course_id == season.course_id, Hole.nine == week.nine)
        .order_by(Hole.number)
    ))


def _resolve_golfers(session: Session, week_id: int, golfer_ids: Sequence[int]) -> dict[int, Candidate]:
    by_golfer: dict[int, list[Candidate]] = {}
    for candidate in card_candidates(session, week_id):
        by_golfer.setdefault(candidate.golfer_id, []).append(candidate)
    resolved: dict[int, Candidate] = {}
    for golfer_id in golfer_ids:
        options = by_golfer.get(golfer_id, [])
        if len(options) != 1:
            raise ScoreEntryError("golfer_not_in_week", "Every golfer must be in this week's matchups.")
        resolved[golfer_id] = options[0]
    return resolved


def _parse_scores(
    entries: Sequence[tuple[int, Mapping[int, object]]], hole_numbers: set[int]
) -> dict[int, dict[int, int]]:
    for _, scores in entries:
        if any(number not in scores or is_blank(scores[number]) for number in hole_numbers):
            raise ScoreEntryError("missing_score", "Every hole needs a score.")
    parsed: dict[int, dict[int, int]] = {}
    for golfer_id, scores in entries:
        if set(scores) != hole_numbers:
            raise ScoreEntryError("bad_score", "Scores must be for this week's holes only.")
        parsed[golfer_id] = {}
        for number, raw in scores.items():
            strokes = validate_strokes(raw)
            if strokes is None:
                raise ScoreEntryError("bad_score", "Each score must be a whole number from 1 to 15.")
            parsed[golfer_id][number] = strokes
    return parsed


def _already_scored(session: Session, player_match_id: int, golfer_id: int) -> bool:
    return session.scalar(
        select(HoleScore.id)
        .where(HoleScore.player_match_id == player_match_id, HoleScore.golfer_id == golfer_id)
        .limit(1)
    ) is not None


def submit_card(
    session: Session,
    week_id: int,
    entries: Sequence[tuple[int, Mapping[int, object]]],
    user_id: int,
    today: date,
    *,
    acknowledged: bool,
) -> CardResult:
    """Validate and write one scorecard; all rows or none, never commits."""
    try:
        reserve_matchup_write(session)
    except (ScheduleConflict, MatchupConflict) as exc:
        raise ScoreEntryError("conflict", "The scorecard is being changed. Roll back and retry.") from exc

    week = week_for_entry(session, week_id, today)

    golfer_ids = [golfer_id for golfer_id, _ in entries]
    if not 2 <= len(golfer_ids) <= 4 or len(set(golfer_ids)) != len(golfer_ids):
        raise ScoreEntryError("golfer_count", "A card needs two to four different golfers.")

    resolved = _resolve_golfers(session, week.id, golfer_ids)
    holes = week_holes(session, week.id)
    hole_by_number = {hole.number: hole for hole in holes}
    parsed = _parse_scores(entries, set(hole_by_number))

    grouping = group_by_match(
        (golfer_id, cand.player_match_id, cand.opponent_golfer_id) for golfer_id, cand in resolved.items()
    )
    if grouping.incomplete and not acknowledged:
        raise ScoreEntryError("needs_acknowledgement", "The card must be acknowledged before submitting.")

    for golfer_id, cand in resolved.items():
        if _already_scored(session, cand.player_match_id, golfer_id):
            raise ScoreEntryError("already_submitted", "This card has already been submitted.")

    rows = [
        HoleScore(
            player_match_id=resolved[golfer_id].player_match_id,
            golfer_id=golfer_id,
            hole_id=hole_by_number[number].id,
            strokes=strokes,
            entered_by_user_id=user_id,
        )
        for golfer_id, scores in parsed.items()
        for number, strokes in sorted(scores.items())
    ]
    try:
        with session.begin_nested():
            session.add_all(rows)
            session.flush()
    except IntegrityError as exc:
        raise ScoreEntryError("already_submitted", "This card has already been submitted.") from exc

    return CardResult(
        week_id=week.id,
        golfer_ids=tuple(golfer_ids),
        player_match_ids=tuple(sorted(grouping.groups)),
        rows_written=len(rows),
        incomplete=grouping.incomplete,
    )


def scores_for_week(session: Session, week_id: int) -> list[MatchScores]:
    """Stored scores grouped by match then golfer with a derived total."""
    holes = {hole.id: hole.number for hole in week_holes(session, week_id)}
    result: list[MatchScores] = []
    for match in _week_player_matches(session, week_id):
        scores = list(session.scalars(
            select(HoleScore).where(HoleScore.player_match_id == match.id).order_by(HoleScore.golfer_id, HoleScore.hole_id)
        ))
        if not scores:
            continue
        per_golfer: dict[int, dict[int, int]] = {}
        for score in scores:
            number = holes.get(score.hole_id)
            if number is None:
                number = session.get(Hole, score.hole_id).number
            per_golfer.setdefault(score.golfer_id, {})[number] = score.strokes
        golfers = tuple(
            GolferScores(
                golfer_id=golfer_id,
                name=_name(session.get(Golfer, golfer_id)),
                strokes_by_hole=dict(sorted(strokes.items())),
                total=sum(strokes.values()),
            )
            for golfer_id, strokes in per_golfer.items()
        )
        result.append(MatchScores(match.id, match.position_label, golfers))
    return result
