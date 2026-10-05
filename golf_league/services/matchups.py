"""Internal matchup helpers: flush only, with caller-owned serialized transactions."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from golf_league.domain.matchups import pairing_shape, validate_team_once_per_week
from golf_league.models import (
    Golfer,
    PlayerMatch,
    Season,
    SeasonGolfer,
    Team,
    TeamMatch,
    TeamMember,
    Week,
)
from golf_league.services.participants import (
    ParticipantValidationError,
    effective_participant,
)
from golf_league.services.schedule import ScheduleConflict, reserve_schedule_write
from golf_league.services.teams import season_matchups_ready

Clock = Callable[[], datetime]


class MatchupValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


class MatchupConflict(Exception):
    """Structural conflicts or retriable database contention."""


def _flush(session: Session) -> None:
    try:
        session.flush()
    except (IntegrityError, OperationalError) as exc:
        raise MatchupConflict("The matchup changed concurrently. Roll back and retry.") from exc


def reserve_matchup_write(session: Session) -> None:
    """Acquire database serialization without committing or rolling back the caller."""
    try:
        reserve_schedule_write(session)
    except ScheduleConflict as exc:
        raise MatchupConflict("The matchup is being changed. Roll back and retry.") from exc
    # Preserve deliberate caller changes before expiring cached roster/seed state.
    _flush(session)
    session.expire_all()


def _members(session, season_id, team_id, validate_golfers=True):
    members = list(session.scalars(select(TeamMember).where(TeamMember.team_id == team_id).order_by(TeamMember.position)))
    if {member.position for member in members} != {1, 2, 3, 4} or len(members) != 4:
        raise MatchupValidationError({"teams": "Every team must have exactly positions 1–4."})
    included = set(session.scalars(select(SeasonGolfer.golfer_id).where(SeasonGolfer.season_id == season_id)))
    for member in members:
        golfer = session.get(Golfer, member.golfer_id)
        if member.season_id != season_id or (validate_golfers and (member.golfer_id not in included or golfer is None or not golfer.is_active)):
            raise MatchupValidationError({"teams": "Every team member must be active and included in the season roster."})
    return {member.position: member.golfer_id for member in members}


def _context(session, week_id, home_team_id, away_team_id, validate_golfers=True):
    week = session.get(Week, week_id)
    if week is None:
        raise LookupError("Week not found.")
    season = session.get(Season, week.season_id)
    home, away = session.get(Team, home_team_id), session.get(Team, away_team_id)
    if home is None or away is None or home.season_id != week.season_id or away.season_id != week.season_id:
        raise LookupError("Team not found in this season.")
    if week.status != "scheduled" or week.week_type != "match":
        raise MatchupValidationError({"week": "Choose a scheduled match week."})
    if not season_matchups_ready(session, season.id):
        raise MatchupValidationError({"teams": "Every team in the season must have exactly four members."})
    return week, season, _members(session, season.id, home.id, validate_golfers), _members(session, season.id, away.id, validate_golfers)


def _effective(session, season, golfer_id):
    try:
        view = effective_participant(session, season.id, golfer_id)
    except ParticipantValidationError as exc:
        raise MatchupValidationError({"golfer_id": "Every player needs a valid tee on the season course."}) from exc
    if view is None or not view.golfer.is_active:
        raise MatchupValidationError({"golfer_id": "Every player must be an active golfer."})
    if view.effective_tee.course_id != season.course_id:
        raise MatchupValidationError({"golfer_id": "Every player needs a valid tee on the season course."})
    if view.effective_handicap is None:
        raise MatchupValidationError({"handicap": "Every player needs an effective season seed handicap."})
    return view


def _generated_time(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise MatchupValidationError({"generated_at": "The generation clock must return an aware UTC datetime."})
    return value.astimezone(UTC)


def create_team_match(session: Session, week_id: int, home_team_id: int, away_team_id: int,
                      *, clock: Clock) -> tuple[TeamMatch, list[str]]:
    """Create an explicit pairing; the caller commits it with all downstream writes."""
    reserve_matchup_write(session)
    week, season, home_members, away_members = _context(session, week_id, home_team_id, away_team_id)
    existing = list(session.execute(select(TeamMatch.home_team_id, TeamMatch.away_team_id).where(TeamMatch.week_id == week_id)))
    try:
        validate_team_once_per_week(home_team_id, away_team_id, existing)
    except ValueError as exc:
        raise MatchupValidationError({"teams": str(exc)}) from exc
    for golfer_id in set(home_members.values()) | set(away_members.values()):
        _effective(session, season, golfer_id)
    earlier = list(session.scalars(select(Week.index).join(TeamMatch, TeamMatch.week_id == Week.id).where(
        Week.season_id == season.id, Week.index < week.index,
        or_((TeamMatch.home_team_id == home_team_id) & (TeamMatch.away_team_id == away_team_id),
            (TeamMatch.home_team_id == away_team_id) & (TeamMatch.away_team_id == home_team_id)),
    ).order_by(Week.index)))
    warnings = ["This pairing also appears in earlier printed week(s): " + ", ".join(map(str, earlier)) + "."] if earlier else []
    sort_order = session.scalar(select(func.max(TeamMatch.sort_order)).where(TeamMatch.week_id == week_id))
    pairing = TeamMatch(week_id=week.id, home_team_id=home_team_id, away_team_id=away_team_id,
                        is_self_match=home_team_id == away_team_id, sort_order=(sort_order or 0) + 1)
    session.add(pairing)
    _flush(session)
    return pairing, warnings


def _generated_participant(session: Session, season: Season, golfer_id: int):
    selected = session.scalar(select(SeasonGolfer.id).where(
        SeasonGolfer.season_id == season.id, SeasonGolfer.golfer_id == golfer_id,
    ))
    if selected is None:
        raise MatchupValidationError({"teams": "Generated players must be included in the season roster."})
    return _effective(session, season, golfer_id)


def _generate_player_matches(session: Session, team_match_id: int, *, clock: Clock) -> list[PlayerMatch]:
    """Validate all slots before writing; keep IDs/manual rows and unchanged times."""
    reserve_matchup_write(session)
    pairing = session.get(TeamMatch, team_match_id)
    if pairing is None:
        raise LookupError("Team pairing not found.")
    _, season, home_members, away_members = _context(session, pairing.week_id, pairing.home_team_id, pairing.away_team_id, False)
    others = list(session.execute(select(TeamMatch.home_team_id, TeamMatch.away_team_id).where(
        TeamMatch.week_id == pairing.week_id, TeamMatch.id != pairing.id,
    )))
    try:
        validate_team_once_per_week(pairing.home_team_id, pairing.away_team_id, others)
    except ValueError as exc:
        raise MatchupValidationError({"teams": str(exc)}) from exc
    shape = pairing_shape(pairing.home_team_id, pairing.away_team_id)
    existing = {row.position_label: row for row in session.scalars(select(PlayerMatch).where(PlayerMatch.team_match_id == pairing.id))}
    expected_labels = {label for label, *_ in shape}
    invalid_slots = sorted(existing.keys() - expected_labels)
    if invalid_slots:
        protected = ", ".join(label for label in invalid_slots if existing[label].manually_adjusted)
        raise MatchupConflict("Existing slots no longer fit the pairing: " + ", ".join(invalid_slots) +
                              (". Protected slots: " + protected if protected else ". Historical IDs must be preserved."))
    desired = []
    for label, home_position, away_position, own in shape:
        row = existing.get(label)
        if row is not None and row.manually_adjusted:
            if row.vs_own_handicap != own:
                raise MatchupConflict(f"Protected slot {label} no longer fits the pairing kind.")
            desired.append((label, row, None))
        else:
            a, b = home_members[home_position], away_members[away_position]
            _generated_participant(session, season, a)
            _generated_participant(session, season, b)
            desired.append((label, row, (a, b, False, False, own)))
    changes = [(label, row, values) for label, row, values in desired if values is not None and
               (row is None or (row.a_golfer_id, row.b_golfer_id, row.a_is_sub, row.b_is_sub, row.vs_own_handicap) != values)]
    now = _generated_time(clock) if changes else None
    for label, row, values in changes:
        if row is None:
            row = PlayerMatch(team_match_id=pairing.id, position_label=label, manually_adjusted=False)
            session.add(row)
            existing[label] = row
        row.a_golfer_id, row.b_golfer_id, row.a_is_sub, row.b_is_sub, row.vs_own_handicap = values
        row.generated_at = now
    _flush(session)
    return [existing[label] for label, *_ in shape]


def generate_week_matches(session: Session, season_id: int, *, clock: Clock,
                          team_match_id: int | None = None, week_id: int | None = None,
                          home_team_id: int | None = None, away_team_id: int | None = None):
    """One public commit boundary for pairing/player/snapshot creation or regeneration."""
    from golf_league.services.handicaps import ensure_week_handicap

    try:
        reserve_matchup_write(session)
        if team_match_id is not None:
            pairing = session.get(TeamMatch, team_match_id)
            week = session.get(Week, pairing.week_id) if pairing else None
            if week is None or week.season_id != season_id:
                raise LookupError("Pairing not found in this season.")
            warnings = []
        else:
            week = session.get(Week, week_id)
            if week is None or week.season_id != season_id:
                raise LookupError("Week not found in this season.")
            pairing, warnings = create_team_match(session, week_id, home_team_id, away_team_id, clock=clock)
        rows = _generate_player_matches(session, pairing.id, clock=clock)
        for row in rows:
            if not row.manually_adjusted:
                for golfer_id in (row.a_golfer_id, row.b_golfer_id):
                    ensure_week_handicap(session, pairing.week_id, golfer_id, clock=clock)
        session.commit()
        return pairing, rows, warnings
    except Exception:
        session.rollback()
        raise


def generate_player_matches(session: Session, team_match_id: int, *, clock: Clock) -> list[PlayerMatch]:
    """Supported committing generation always ensures snapshots."""
    try:
        reserve_matchup_write(session)
        pairing = session.get(TeamMatch, team_match_id)
        if pairing is None:
            raise LookupError("Pairing not found.")
        week = session.get(Week, pairing.week_id)
        return generate_week_matches(session, week.season_id, team_match_id=team_match_id, clock=clock)[1]
    except Exception:
        session.rollback()
        raise


def substitute_player_core(session: Session, season_id: int, player_match_id: int, *, side: str,
                           golfer_id: int, clock: Clock) -> PlayerMatch:
    """Flush-only serialized substitution; caller owns the snapshot transaction."""
    from golf_league.services.handicaps import ensure_week_handicap

    reserve_matchup_write(session)
    row = session.get(PlayerMatch, player_match_id)
    pairing = session.get(TeamMatch, row.team_match_id) if row else None
    week = session.get(Week, pairing.week_id) if pairing else None
    if week is None or week.season_id != season_id:
        raise LookupError("Player match not found in this season.")
    if side not in {"a", "b"}:
        raise MatchupValidationError({"side": "Choose side a or b."})
    if week.status != "scheduled" or week.week_type != "match":
        raise MatchupValidationError({"week": "Choose a scheduled match week."})
    if getattr(row, f"{side}_golfer_id") == golfer_id and getattr(row, f"{side}_is_sub"):
        return row
    golfer = session.get(Golfer, golfer_id)
    if golfer is None:
        raise LookupError("Golfer not found.")
    if not golfer.is_active or session.scalar(select(TeamMember.id).where(
        TeamMember.season_id == season_id, TeamMember.golfer_id == golfer_id,
    ).limit(1)) is not None:
        raise MatchupValidationError({"golfer_id": "Choose an active substitute with no team slot in this season."})
    for match in session.scalars(select(PlayerMatch).join(TeamMatch).where(TeamMatch.week_id == week.id)):
        for occupied_side in ("a", "b"):
            if getattr(match, f"{occupied_side}_golfer_id") == golfer_id:
                raise MatchupConflict("This substitute already replaces a player in this week.")
    ensure_week_handicap(session, week.id, golfer_id, clock=clock)
    setattr(row, f"{side}_golfer_id", golfer_id)
    setattr(row, f"{side}_is_sub", True)
    row.manually_adjusted = True
    _flush(session)
    return row


def substitute_player(session: Session, season_id: int, player_match_id: int, *, side: str,
                      golfer_id: int, clock: Clock) -> PlayerMatch:
    try:
        row = substitute_player_core(session, season_id, player_match_id, side=side, golfer_id=golfer_id, clock=clock)
        session.commit()
        return row
    except Exception:
        session.rollback()
        raise
