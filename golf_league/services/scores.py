"""Score entry: which weeks take scores, who may write them, and the one write path.

Three ways in (a card, a typed form for one match, the admin week sheet) share
these rules (R-SCORES). Every write runs on the caller's session, reserves the
single SQLite writer first, flushes, and never commits, rolls back or reads the
clock: the caller owns the transaction and passes `today` and `now`. Every
refusal writes nothing.

A match's scores can be changed until an admin locks the match. Each match
that has scores or a lock has one `match_score_states` row; its revision lets
a form tell whether what it showed is still what is saved.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.scorecard import (
    Actor,
    group_by_match,
    is_blank,
    validate_strokes,
)
from golf_league.models import (
    Golfer,
    Hole,
    HoleScore,
    MatchScoreState,
    PlayerMatch,
    Season,
    Team,
    TeamMatch,
    TeamMember,
    User,
    Week,
    WeekHandicap,
)
from golf_league.services.matchups import Clock, MatchupConflict, reserve_matchup_write
from golf_league.services.schedule import ScheduleConflict

ERROR_CODES = (
    "not_found", "cancelled", "played", "no_matchup", "not_open_yet", "conflict",
    "golfer_count", "golfer_not_in_week", "card_scope", "missing_score", "bad_score",
    "needs_acknowledgement", "left_off", "nothing_left", "not_allowed", "locked", "out_of_date",
)
# How far back the admin pages look for a week that still needs scores.
DEFAULT_WEEK_LOOKBACK = timedelta(days=6)


class ScoreEntryError(Exception):
    """A refused score operation with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, golfer_ids: tuple[int, ...] = (),
                 player_match_id: int | None = None):
        self.code = code
        self.message = message
        self.golfer_ids = golfer_ids
        self.player_match_id = player_match_id
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
    left_off: tuple[int, ...] = ()


@dataclass(frozen=True)
class MatchSaveResult:
    player_match_id: int
    revision: int
    rows_inserted: int
    rows_updated: int


@dataclass(frozen=True)
class LockWeekResult:
    """Match IDs by what "Lock all" did with them."""

    locked: tuple[int, ...]
    already_locked: tuple[int, ...]
    incomplete: tuple[int, ...]
    changed: tuple[int, ...]


@dataclass(frozen=True)
class SheetGolfer:
    golfer_id: int
    name: str
    is_sub: bool
    strokes_by_hole: dict[int, int]
    total: int | None  # None when the golfer has no scores
    handicap: int | None  # the week's stored strokes for this golfer, or None
    position: int | None  # the golfer's position (1-4) on their team this season, or None
    entered_by_user_id: int | None
    entered_by_name: str | None
    team_name: str  # the team this golfer plays for this week
    team_side: str  # "home" or "away", used to colour the team cell


@dataclass(frozen=True)
class SheetMatch:
    player_match_id: int
    team_match_id: int
    home_team_name: str
    away_team_name: str
    position_label: str
    golfers: tuple[SheetGolfer, SheetGolfer]  # side a, then side b
    revision: int
    locked: bool
    locked_at: datetime | None
    locked_by_name: str | None
    scores_changed_at: datetime | None
    scores_changed_by_name: str | None
    complete: bool  # both golfers have every hole
    empty: bool  # neither golfer has a score


@dataclass(frozen=True)
class EntryState:
    """Where a player stands for a week: see `player_entry_state`."""

    state: str
    player_match_id: int | None = None
    opens_on: date | None = None


def league_today(clock: Clock, timezone_name: str) -> date:
    """League-local calendar date for the injected clock's aware instant."""
    return clock().astimezone(ZoneInfo(timezone_name)).date()


# --- reading: matches, holes and state ---------------------------------------


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


def _plays_in_week(session: Session, week_id: int, golfer_id: int | None) -> bool:
    """Whether the golfer is a side of one of the week's matches."""
    if golfer_id is None:
        return False
    return session.scalar(
        select(PlayerMatch.id)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id,
               (PlayerMatch.a_golfer_id == golfer_id) | (PlayerMatch.b_golfer_id == golfer_id))
        .limit(1)
    ) is not None


def _match_in_week(session: Session, week_id: int, player_match_id: int) -> PlayerMatch | None:
    return session.scalar(
        select(PlayerMatch)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id, PlayerMatch.id == player_match_id)
    )


def _require_match(session: Session, week_id: int, player_match_id: int) -> PlayerMatch:
    match = _match_in_week(session, week_id, player_match_id)
    if match is None:
        raise ScoreEntryError("not_found", "That match is not in this week.", player_match_id=player_match_id)
    return match


def _state(session: Session, player_match_id: int) -> MatchScoreState | None:
    return session.get(MatchScoreState, player_match_id)


def _revision(state: MatchScoreState | None) -> int:
    return state.revision if state is not None else 0


def _is_locked(state: MatchScoreState | None) -> bool:
    return state is not None and state.locked_at is not None


def _rows_by_golfer(session: Session, player_match_id: int, *,
                    refresh: bool = False) -> dict[int, list[HoleScore]]:
    """A match's saved rows by golfer; `refresh` reads every value from the database again."""
    rows: dict[int, list[HoleScore]] = {}
    query = select(HoleScore).where(HoleScore.player_match_id == player_match_id)
    if refresh:
        query = query.execution_options(populate_existing=True)
    for row in session.scalars(query):
        rows.setdefault(row.golfer_id, []).append(row)
    return rows


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


# --- which week ---------------------------------------------------------------


def week_for_entry(session: Session, week_id: int, today: date, actor: Actor) -> Week:
    """Return the week or raise ScoreEntryError; performs reads only.

    Only a scheduled match week with generated matches takes scores. A player
    must be linked to a golfer who is a side of one of the week's matches, and
    may enter from the week's play date onward with no end date; an admin has
    no date limit. The lock on a match is the only cutoff.
    """
    week = session.get(Week, week_id)
    if week is None:
        raise ScoreEntryError("not_found", "That week does not exist.")
    if week.status == "cancelled":
        raise ScoreEntryError("cancelled", "That week is cancelled.")
    if week.status == "played":
        raise ScoreEntryError("played", "That week is already played.")
    if week.week_type != "match":
        raise ScoreEntryError("no_matchup", "That week is not a match week.")
    if not _has_matches(session, week.id):
        raise ScoreEntryError("no_matchup", "That week has no generated matches.")
    if not actor.is_admin and not _plays_in_week(session, week.id, actor.golfer_id):
        raise ScoreEntryError("not_allowed", "This account cannot enter scores for that week.")
    if not actor.is_admin and week.play_date > today:
        raise ScoreEntryError("not_open_yet", "Scores for that week cannot be entered yet.")
    return week


def eligible_weeks(session: Session, season_id: int, today: date, actor: Actor) -> list[Week]:
    """Scheduled match weeks with generated matches, by date then index.

    For a player: only weeks they play in, and not future ones.
    """
    query = select(Week).where(Week.season_id == season_id, Week.status == "scheduled",
                               Week.week_type == "match")
    if not actor.is_admin:
        query = query.where(Week.play_date <= today)
    weeks = session.scalars(query.order_by(Week.play_date, Week.index))
    return [week for week in weeks if _has_matches(session, week.id)
            and (actor.is_admin or _plays_in_week(session, week.id, actor.golfer_id))]


def _has_open_match(session: Session, week_id: int, hole_count: int) -> bool:
    """An open match is an unlocked match in which at least one golfer has no saved scores."""
    for match in _week_player_matches(session, week_id):
        if _is_locked(_state(session, match.id)):
            continue
        rows = _rows_by_golfer(session, match.id)
        if any(len(rows.get(golfer_id, ())) < max(hole_count, 1)
               for golfer_id in (match.a_golfer_id, match.b_golfer_id)):
            return True
    return False


def default_week(session: Session, season_id: int, today: date) -> Week | None:
    """The week an admin most likely means (R-SCORES "The default week").

    Today's week; else the latest week of the last six days that still has an
    open match; else the next week; else None.
    """
    weeks = eligible_weeks(session, season_id, today, Actor(0, True))
    for week in weeks:
        if week.play_date == today:
            return week
    recent = [week for week in weeks if today - DEFAULT_WEEK_LOOKBACK <= week.play_date < today]
    for week in reversed(recent):
        if _has_open_match(session, week.id, len(week_holes(session, week.id))):
            return week
    return next((week for week in weeks if week.play_date > today), None)


# --- who may write what -------------------------------------------------------


def writable_golfers(session: Session, match: PlayerMatch, actor: Actor) -> frozenset[int]:
    """The golfers of a match whose scores this person may enter or change.

    An admin, or a player who is a side of the match: both golfers. Any other
    player: only a golfer whose saved scores they first entered themselves (the
    scorekeeper's right over the card they submitted). A player has no rights
    at all unless their account is linked to a golfer who plays in the week.
    """
    sides = (match.a_golfer_id, match.b_golfer_id)
    if actor.is_admin:
        return frozenset(sides)
    week_id = session.get(TeamMatch, match.team_match_id).week_id
    if not _plays_in_week(session, week_id, actor.golfer_id):
        return frozenset()
    if actor.golfer_id in sides:
        return frozenset(sides)
    rows = _rows_by_golfer(session, match.id)
    return frozenset(
        golfer_id for golfer_id in sides
        if rows.get(golfer_id) and all(row.entered_by_user_id == actor.user_id for row in rows[golfer_id])
    )


def kept_matches(session: Session, week_id: int, actor: Actor) -> list[int]:
    """Matches of the week, other than the player's own, in which the player entered a score."""
    kept = []
    for match in _week_player_matches(session, week_id):
        if actor.golfer_id in (match.a_golfer_id, match.b_golfer_id):
            continue
        if session.scalar(select(HoleScore.id).where(
                HoleScore.player_match_id == match.id,
                HoleScore.entered_by_user_id == actor.user_id).limit(1)) is not None:
            kept.append(match.id)
    return kept


def blocked_golfers(session: Session, week_id: int, golfer_ids: Iterable[int]) -> frozenset[int]:
    """The golfers who already have saved scores for their match in the week, or whose match is locked."""
    wanted = set(golfer_ids)
    blocked: set[int] = set()
    for match in _week_player_matches(session, week_id):
        sides = wanted & {match.a_golfer_id, match.b_golfer_id}
        if not sides:
            continue
        if _is_locked(_state(session, match.id)):
            blocked |= sides
            continue
        rows = _rows_by_golfer(session, match.id)
        blocked |= {golfer_id for golfer_id in sides if rows.get(golfer_id)}
    return frozenset(blocked)


# --- the shared write steps ---------------------------------------------------


def _reserve(session: Session) -> None:
    try:
        reserve_matchup_write(session)
    except (ScheduleConflict, MatchupConflict) as exc:
        raise ScoreEntryError("conflict", "Scores are being changed. Nothing was saved; try again.") from exc


@contextmanager
def _writing(session: Session) -> Iterator[None]:
    """Make the changes inside a savepoint and flush them.

    A refusal or a key violation undoes only this savepoint, so the caller's
    transaction is never left unusable; a key violation becomes `conflict`.
    """
    try:
        with session.begin_nested():
            yield
            session.flush()
    except IntegrityError as exc:
        raise ScoreEntryError("conflict", "Scores are being changed. Nothing was saved; try again.") from exc


def _bump(session: Session, player_match_id: int, actor: Actor, now: datetime, *,
          scores_changed: bool) -> MatchScoreState:
    """Raise a match's revision (a new row starts at 1) and record a change to its scores."""
    state = _state(session, player_match_id)
    if state is None:
        state = MatchScoreState(player_match_id=player_match_id, revision=1)
        session.add(state)
    else:
        state.revision += 1
    if scores_changed:
        state.scores_changed_at = now
        state.scores_changed_by_user_id = actor.user_id
    return state


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


def _parse_scores(entries: Iterable[tuple[int, Mapping[int, object]]],
                  hole_numbers: set[int]) -> dict[int, dict[int, int]]:
    entries = list(entries)
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


def _check_player_card(session: Session, week_id: int, actor: Actor,
                       resolved: Mapping[int, Candidate]) -> None:
    """A player's card: their own golfer, their own match, and at most one other match a week."""
    if actor.golfer_id is None:
        raise ScoreEntryError("not_allowed", "This account is not linked to a golfer.")
    if actor.golfer_id not in resolved:
        raise ScoreEntryError("card_scope", "Your own name must be on the card.")
    own_match = resolved[actor.golfer_id].player_match_id
    others = {candidate.player_match_id for candidate in resolved.values()} - {own_match}
    if len(others) > 1:
        raise ScoreEntryError("card_scope", "A card can hold your match and one other match.")
    if len(others | set(kept_matches(session, week_id, actor))) > 1:
        raise ScoreEntryError("card_scope", "You have already kept score for another match this week.")


# --- a card -------------------------------------------------------------------


def submit_card(session: Session, week_id: int, entries: Sequence[tuple[int, Mapping[int, object]]],
                actor: Actor, today: date, *, acknowledged: bool,
                left_off: frozenset[int] = frozenset(), now: datetime) -> CardResult:
    """Validate and write one scorecard; a card only adds, and never commits.

    A golfer who already has saved scores, or whose match is locked, is left
    off. The card is refused (`left_off`) until it is submitted again naming
    exactly those golfers in `left_off`, so nobody is dropped unseen.
    """
    _reserve(session)
    week = week_for_entry(session, week_id, today, actor)

    golfer_ids = [golfer_id for golfer_id, _ in entries]
    if not 2 <= len(golfer_ids) <= 4 or len(set(golfer_ids)) != len(golfer_ids):
        raise ScoreEntryError("golfer_count", "A card needs two to four different golfers.")
    resolved = _resolve_golfers(session, week.id, golfer_ids)
    if not actor.is_admin:
        _check_player_card(session, week.id, actor, resolved)

    hole_by_number = {hole.number: hole for hole in week_holes(session, week.id)}
    parsed = _parse_scores(entries, set(hole_by_number))

    grouping = group_by_match(
        (golfer_id, cand.player_match_id, cand.opponent_golfer_id) for golfer_id, cand in resolved.items()
    )
    if grouping.incomplete and not acknowledged:
        raise ScoreEntryError("needs_acknowledgement", "The card must be acknowledged before submitting.")

    blocked = blocked_golfers(session, week.id, golfer_ids)
    blocked_ids = tuple(golfer_id for golfer_id in golfer_ids if golfer_id in blocked)
    if blocked == frozenset(golfer_ids):
        raise ScoreEntryError("nothing_left", "Every golfer on this card already has saved scores "
                              "or is in a locked match.", golfer_ids=blocked_ids)
    if blocked != left_off:
        raise ScoreEntryError("left_off", "Some golfers on this card already have saved scores "
                              "or are in a locked match.", golfer_ids=blocked_ids)

    written = [golfer_id for golfer_id in golfer_ids if golfer_id not in blocked]
    with _writing(session):
        session.add_all(
            HoleScore(player_match_id=resolved[golfer_id].player_match_id, golfer_id=golfer_id,
                      hole_id=hole_by_number[number].id, strokes=strokes,
                      entered_by_user_id=actor.user_id)
            for golfer_id in written for number, strokes in sorted(parsed[golfer_id].items())
        )
        for player_match_id in sorted({resolved[golfer_id].player_match_id for golfer_id in written}):
            _bump(session, player_match_id, actor, now, scores_changed=True)
    return CardResult(
        week_id=week.id, golfer_ids=tuple(written),
        player_match_ids=tuple(sorted({resolved[golfer_id].player_match_id for golfer_id in written})),
        rows_written=len(written) * len(hole_by_number), incomplete=grouping.incomplete,
        left_off=blocked_ids,
    )


# --- one match ----------------------------------------------------------------


def _apply_scores(session: Session, player_match_id: int, parsed: Mapping[int, Mapping[int, int]],
                  hole_by_number: Mapping[int, Hole], actor: Actor) -> tuple[int, int]:
    """Insert a golfer's missing rows and update strokes that differ; returns (inserted, updated)."""
    existing = _rows_by_golfer(session, player_match_id)
    inserted = updated = 0
    for golfer_id, strokes_by_hole in parsed.items():
        saved = {row.hole_id: row for row in existing.get(golfer_id, [])}
        for number, strokes in sorted(strokes_by_hole.items()):
            row = saved.get(hole_by_number[number].id)
            if row is None:
                session.add(HoleScore(player_match_id=player_match_id, golfer_id=golfer_id,
                                      hole_id=hole_by_number[number].id, strokes=strokes,
                                      entered_by_user_id=actor.user_id))
                inserted += 1
            elif row.strokes != strokes:
                # Only the strokes change: who first entered the score, and when, stay.
                row.strokes = strokes
                updated += 1
    return inserted, updated


def _save_match(session: Session, week: Week, player_match_id: int,
                scores: Mapping[int, Mapping[int, object]], actor: Actor, *, base_revision: int | None,
                now: datetime) -> MatchSaveResult:
    """Checks 3 to 9 of a match save; the writer is already reserved and the week checked."""
    match = _match_in_week(session, week.id, player_match_id)
    if match is None:
        raise ScoreEntryError("not_found", "That match is not in this week.", player_match_id=player_match_id)
    writable = writable_golfers(session, match, actor)
    if not writable:
        raise ScoreEntryError("not_allowed", "You cannot change this match's scores.",
                              player_match_id=player_match_id)
    state = _state(session, match.id)
    if _is_locked(state):
        raise ScoreEntryError("locked", "This match is locked.", player_match_id=player_match_id)
    if _revision(state) != base_revision:
        raise ScoreEntryError("out_of_date", "These scores were changed after the form was shown.",
                              player_match_id=player_match_id)

    hole_by_number = {hole.number: hole for hole in week_holes(session, week.id)}
    ordered = [golfer_id for golfer_id in (match.a_golfer_id, match.b_golfer_id) if golfer_id in writable]
    try:
        parsed = _parse_scores(((golfer_id, scores.get(golfer_id, {})) for golfer_id in ordered),
                               set(hole_by_number))
    except ScoreEntryError as exc:
        raise ScoreEntryError(exc.code, exc.message, player_match_id=player_match_id) from None

    inserted, updated = _apply_scores(session, match.id, parsed, hole_by_number, actor)
    if inserted or updated:
        state = _bump(session, match.id, actor, now, scores_changed=True)
    return MatchSaveResult(match.id, _revision(state), inserted, updated)


def save_match_scores(session: Session, week_id: int, player_match_id: int,
                      scores: Mapping[int, Mapping[int, object]], actor: Actor, today: date, *,
                      base_revision: int | None, now: datetime) -> MatchSaveResult:
    """Enter or change one match's scores. `scores` maps golfer ID to `{hole number: value}`.

    Golfers in `scores` whom this person may not write are ignored. A save that
    changes nothing writes nothing and leaves the revision alone.
    """
    _reserve(session)
    week = week_for_entry(session, week_id, today, actor)
    with _writing(session):
        return _save_match(session, week, player_match_id, scores, actor,
                           base_revision=base_revision, now=now)


def save_week(session: Session, week_id: int,
              matches: Sequence[tuple[int, Mapping[int, Mapping[int, object]], int | None]],
              actor: Actor, today: date, *, now: datetime) -> list[MatchSaveResult]:
    """The admin week sheet: every match given is saved, or none is.

    `matches` holds `(player_match_id, scores, base_revision)`. The first
    refusal raises with `player_match_id` set; the caller rolls back.
    """
    if not actor.is_admin:
        raise ScoreEntryError("not_allowed", "Only an admin can save a whole week.")
    _reserve(session)
    week = week_for_entry(session, week_id, today, actor)
    with _writing(session):
        return [
            _save_match(session, week, player_match_id, scores, actor, base_revision=base_revision, now=now)
            for player_match_id, scores, base_revision in matches
        ]


# --- lock, unlock, clear ------------------------------------------------------


def _require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise ScoreEntryError("not_allowed", "Only an admin can do this.")


def _lock(session: Session, player_match_id: int, actor: Actor, now: datetime) -> int:
    state = _bump(session, player_match_id, actor, now, scores_changed=False)
    state.locked_at, state.locked_by_user_id = now, actor.user_id
    return state.revision


def lock_match(session: Session, week_id: int, player_match_id: int, actor: Actor, *,
               base_revision: int | None, now: datetime) -> int:
    """Lock one match in any state, even with no scores; returns its revision."""
    _require_admin(actor)
    _reserve(session)
    match = _require_match(session, week_id, player_match_id)
    state = _state(session, match.id)
    if _is_locked(state):
        return state.revision
    if _revision(state) != base_revision:
        raise ScoreEntryError("out_of_date", "This match changed after the page was shown.",
                              player_match_id=player_match_id)
    with _writing(session):
        return _lock(session, match.id, actor, now)


def unlock_match(session: Session, week_id: int, player_match_id: int, actor: Actor, *,
                 now: datetime) -> int:
    """Unlock one match; no change when it is not locked. Returns its revision."""
    _require_admin(actor)
    _reserve(session)
    match = _require_match(session, week_id, player_match_id)
    state = _state(session, match.id)
    if not _is_locked(state):
        return _revision(state)
    with _writing(session):
        state.revision += 1
        state.locked_at = state.locked_by_user_id = None
    return state.revision


def lock_week(session: Session, week_id: int, actor: Actor, revisions: Mapping[int, int], *,
              now: datetime) -> LockWeekResult:
    """"Lock all": lock each complete, unlocked match that is unchanged since the page was shown.

    `revisions` maps match ID to the revision the page showed. A match that is
    not fully entered, or that changed meanwhile, is left open and reported.
    """
    _require_admin(actor)
    _reserve(session)
    if session.get(Week, week_id) is None:
        raise ScoreEntryError("not_found", "That week does not exist.")
    hole_count = len(week_holes(session, week_id))
    locked, already, incomplete, changed = [], [], [], []
    with _writing(session):
        for match in _week_player_matches(session, week_id):
            state = _state(session, match.id)
            rows = _rows_by_golfer(session, match.id)
            if _is_locked(state):
                already.append(match.id)
            elif hole_count == 0 or any(len(rows.get(golfer_id, ())) != hole_count
                                        for golfer_id in (match.a_golfer_id, match.b_golfer_id)):
                incomplete.append(match.id)
            elif revisions.get(match.id) != _revision(state):
                changed.append(match.id)
            else:
                _lock(session, match.id, actor, now)
                locked.append(match.id)
    return LockWeekResult(tuple(locked), tuple(already), tuple(incomplete), tuple(changed))


def clear_match_scores(session: Session, week_id: int, player_match_id: int, actor: Actor, *,
                       base_revision: int | None, now: datetime) -> int:
    """Delete every saved score of one unlocked match; returns how many rows were deleted."""
    _require_admin(actor)
    _reserve(session)
    match = _require_match(session, week_id, player_match_id)
    state = _state(session, match.id)
    if _is_locked(state):
        raise ScoreEntryError("locked", "This match is locked.", player_match_id=player_match_id)
    if _revision(state) != base_revision:
        raise ScoreEntryError("out_of_date", "This match changed after the page was shown.",
                              player_match_id=player_match_id)
    rows = list(session.scalars(select(HoleScore).where(HoleScore.player_match_id == match.id)))
    if not rows:
        return 0
    with _writing(session):
        for row in rows:
            session.delete(row)
        _bump(session, match.id, actor, now, scores_changed=True)
    return len(rows)


# --- what a page shows --------------------------------------------------------


def week_sheet(session: Session, week_id: int) -> list[SheetMatch]:
    """Every match of the week in schedule order, with its scores, last change and lock.

    A page's form carries the revision shown here, so it must never be newer
    than the scores shown beside it. These reads run outside a transaction, so
    for each match the state is read from the database first and the scores
    after it: a save landing in between pairs newer scores with an older
    revision, which the write then refuses as out of date, never the reverse.
    """
    week = session.get(Week, week_id)
    hole_number = {hole.id: hole.number for hole in week_holes(session, week_id)}
    handicaps = {
        row.golfer_id: row.strokes
        for row in session.scalars(select(WeekHandicap).where(WeekHandicap.week_id == week_id))
    }
    matches = session.execute(
        select(PlayerMatch, TeamMatch)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id)
        .order_by(TeamMatch.sort_order, TeamMatch.id, PlayerMatch.id)
    ).all()
    user_names: dict[int, str | None] = {}

    def user_name(user_id: int | None) -> str | None:
        if user_id is None:
            return None
        if user_id not in user_names:
            user = session.get(User, user_id)
            user_names[user_id] = user.display_name if user is not None else None
        return user_names[user_id]

    sheet: list[SheetMatch] = []
    # One batch read of every team member's position for the teams playing this week,
    # keyed by (team_id, golfer_id), so each golfer's row can show their position.
    member_position: dict[tuple[int, int], int] = {}
    team_ids: set[int] = set()
    for _match, _pairing in matches:
        team_ids.add(_pairing.home_team_id)
        team_ids.add(_pairing.away_team_id)
    if team_ids:
        for _member in session.scalars(
            select(TeamMember).where(
                TeamMember.team_id.in_(team_ids), TeamMember.season_id == week.season_id
            )
        ):
            member_position[(_member.team_id, _member.golfer_id)] = _member.position
    for match, pairing in matches:
        state = session.get(MatchScoreState, match.id, populate_existing=True)  # first; see above
        revision, locked = _revision(state), _is_locked(state)
        rows = _rows_by_golfer(session, match.id, refresh=True)
        home_team_name = session.get(Team, pairing.home_team_id).name
        away_team_name = session.get(Team, pairing.away_team_id).name
        golfers = []
        for golfer_id, is_sub, team_id in (
            (match.a_golfer_id, match.a_is_sub, pairing.home_team_id),
            (match.b_golfer_id, match.b_is_sub, pairing.away_team_id),
        ):
            saved = rows.get(golfer_id, [])
            strokes = dict(sorted(
                (hole_number.get(row.hole_id) or session.get(Hole, row.hole_id).number, row.strokes)
                for row in saved
            ))
            entered_by = saved[0].entered_by_user_id if saved else None
            is_home = team_id == pairing.home_team_id
            golfers.append(SheetGolfer(
                golfer_id=golfer_id, name=_name(session.get(Golfer, golfer_id)), is_sub=bool(is_sub),
                strokes_by_hole=strokes, total=sum(strokes.values()) if saved else None,
                handicap=handicaps.get(golfer_id),
                position=member_position.get((team_id, golfer_id)),
                entered_by_user_id=entered_by, entered_by_name=user_name(entered_by),
                team_name=home_team_name if is_home else away_team_name,
                team_side="home" if is_home else "away",
            ))
        counts = [len(golfer.strokes_by_hole) for golfer in golfers]
        sheet.append(SheetMatch(
            player_match_id=match.id, team_match_id=pairing.id,
            home_team_name=home_team_name,
            away_team_name=away_team_name,
            position_label=match.position_label, golfers=(golfers[0], golfers[1]),
            revision=revision, locked=locked,
            locked_at=state.locked_at if state else None,
            locked_by_name=user_name(state.locked_by_user_id) if state else None,
            scores_changed_at=state.scores_changed_at if state else None,
            scores_changed_by_name=user_name(state.scores_changed_by_user_id) if state else None,
            complete=bool(hole_number) and all(count == len(hole_number) for count in counts),
            empty=not any(counts),
        ))
    return sheet


def player_entry_state(session: Session, week_id: int, golfer_id: int | None, today: date) -> EntryState:
    """Where a player stands for a week.

    `not_playing` (no golfer, unknown week, or not a side of a match in it);
    `closed` (the week is cancelled or played, or is no longer a match week); `not_open_yet` (before the play
    date); `locked`; `saved` (both golfers have every score, not locked);
    `open` (anything else).
    """
    week = session.get(Week, week_id)
    if golfer_id is None or week is None:
        return EntryState("not_playing")
    match = next((row for row in _week_player_matches(session, week.id)
                  if golfer_id in (row.a_golfer_id, row.b_golfer_id)), None)
    if match is None:
        return EntryState("not_playing")
    if week.status != "scheduled" or week.week_type != "match":  # as `week_for_entry`
        return EntryState("closed", match.id)
    if week.play_date > today:
        return EntryState("not_open_yet", match.id, week.play_date)
    if _is_locked(_state(session, match.id)):
        return EntryState("locked", match.id)
    hole_count = len(week_holes(session, week.id))
    rows = _rows_by_golfer(session, match.id)
    if hole_count and all(len(rows.get(side, ())) == hole_count
                          for side in (match.a_golfer_id, match.b_golfer_id)):
        return EntryState("saved", match.id)
    return EntryState("open", match.id)


def player_entry_states(session: Session, week_ids: Iterable[int], golfer_id: int | None,
                        today: date) -> dict[int, EntryState]:
    return {week_id: player_entry_state(session, week_id, golfer_id, today) for week_id in week_ids}
