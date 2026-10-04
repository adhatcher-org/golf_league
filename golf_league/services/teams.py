"""Session-taking operations for manual season teams."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.teams import order_members_by_seed, season_ready_for_matchups
from golf_league.models import Golfer, Season, Team, TeamMember
from golf_league.services.participants import (
    ParticipantValidationError,
    effective_participant,
)


class TeamValidationError(Exception):
    """Invalid team input, represented by field-level messages."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


@dataclass(frozen=True)
class TeamView:
    team: Team
    members: list[TeamMember]
    complete: bool


def _parse_integer(value: object, field: str, label: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise TeamValidationError({field: f"{label} must be a whole number."})
    text = str(value).strip()
    if not text or not text.isdecimal():
        raise TeamValidationError({field: f"{label} must be a whole number."})
    number = int(text)
    if number < minimum:
        raise TeamValidationError({field: f"{label} must be at least {minimum}."})
    return number


def _parse_members(members: object) -> list[tuple[int, int]]:
    if not isinstance(members, (list, tuple)):
        raise TeamValidationError({"members": "Choose one to four team members."})
    if not 1 <= len(members) <= 4:
        raise TeamValidationError({"members": "A team must have one to four members."})
    parsed: list[tuple[int, int]] = []
    golfers: set[int] = set()
    positions: set[int] = set()
    for member in members:
        if not isinstance(member, dict):
            raise TeamValidationError({"members": "Each member needs a golfer and position."})
        golfer_id = _parse_integer(member.get("golfer_id", ""), "golfer_id", "Golfer", 1)
        position = _parse_integer(member.get("position", ""), "position", "Position", 1)
        if position > 4:
            raise TeamValidationError({"position": "Position must be between 1 and 4."})
        if golfer_id in golfers:
            raise TeamValidationError({"golfer_id": "A golfer may appear only once on a team."})
        if position in positions:
            raise TeamValidationError({"position": "Team positions must be unique."})
        golfers.add(golfer_id)
        positions.add(position)
        parsed.append((golfer_id, position))
    return parsed


def _validate_members(session: Session, season: Season, team_id: int | None,
                      members: list[tuple[int, int]]) -> None:
    for golfer_id, _ in members:
        golfer = session.get(Golfer, golfer_id)
        if golfer is None:
            raise TeamValidationError({"golfer_id": "Choose an existing golfer."})
        if not golfer.is_active:
            raise TeamValidationError({"golfer_id": "Choose an active golfer."})
        try:
            effective = effective_participant(session, season.id, golfer.id)
        except ParticipantValidationError:
            effective = None
        if effective is None or effective.effective_tee.course_id != season.course_id:
            raise TeamValidationError({"golfer_id": "Golfer's effective tee must belong to this season's course."})
        if effective.effective_handicap is None:
            raise TeamValidationError({"golfer_id": "Golfer needs an effective seed handicap before assignment."})
        other_team = session.execute(
            select(TeamMember.id).where(
                TeamMember.season_id == season.id,
                TeamMember.golfer_id == golfer.id,
                TeamMember.team_id != team_id if team_id is not None else True,
            ).limit(1)
        ).first()
        if other_team is not None:
            raise TeamValidationError({"golfer_id": "Golfer is already assigned to another team in this season."})


def save_team(session: Session, season_id: int, *, name: object, number: object,
              sort_order: object, members: object, team_id: int | None = None) -> Team | None:
    """Create or replace a team's complete member arrangement atomically."""
    season = session.get(Season, season_id)
    if season is None:
        return None
    team = session.get(Team, team_id) if team_id is not None else None
    if team_id is not None and (team is None or team.season_id != season_id):
        return None
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise TeamValidationError({"name": "Team name is required."})
    if len(normalized_name) > 120:
        raise TeamValidationError({"name": "Team name must be 120 characters or fewer."})
    parsed_number = _parse_integer(number, "number", "Team number", 1)
    parsed_sort_order = _parse_integer(sort_order, "sort_order", "Sort order", 0)
    parsed_members = _parse_members(members)
    _validate_members(session, season, team.id if team else None, parsed_members)

    try:
        if team is None:
            team = Team(season_id=season_id, name=normalized_name, number=parsed_number,
                        sort_order=parsed_sort_order)
            session.add(team)
            session.flush()
        else:
            team.name = normalized_name
            team.number = parsed_number
            team.sort_order = parsed_sort_order
            old_members = list(session.execute(
                select(TeamMember).where(TeamMember.team_id == team.id)
            ).scalars())
            for old_member in old_members:
                session.delete(old_member)
            session.flush()
        session.add_all(
            TeamMember(team_id=team.id, season_id=season_id, golfer_id=golfer_id, position=position)
            for golfer_id, position in parsed_members
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise TeamValidationError({"team": "Team number or membership conflicts with a saved team."}) from None
    assert team is not None
    session.refresh(team)
    return team


def get_team(session: Session, team_id: int) -> Team | None:
    return session.get(Team, team_id)


def list_season_teams(session: Session, season_id: int) -> list[TeamView]:
    if session.get(Season, season_id) is None:
        return []
    teams = list(session.execute(
        select(Team).where(Team.season_id == season_id).order_by(Team.sort_order, Team.number, Team.id)
    ).scalars())
    return [TeamView(team, list(team.members), len(team.members) == 4) for team in teams]


def list_assignable_golfers(session: Session, season_id: int, team_id: int | None = None):
    season = session.get(Season, season_id)
    if season is None:
        return []
    assigned = select(TeamMember.golfer_id).where(
        TeamMember.season_id == season_id,
        TeamMember.team_id != team_id if team_id is not None else True,
    )
    golfers = session.execute(
        select(Golfer).where(Golfer.is_active.is_(True)).order_by(Golfer.last_name, Golfer.first_name, Golfer.id)
    ).scalars()
    result = []
    for golfer in golfers:
        if session.execute(assigned.where(TeamMember.golfer_id == golfer.id).limit(1)).first():
            continue
        try:
            effective = effective_participant(session, season_id, golfer.id)
        except ParticipantValidationError:
            continue
        if (
            effective
            and effective.effective_tee.course_id == season.course_id
            and effective.effective_handicap is not None
        ):
            result.append((golfer, effective.effective_tee, effective.effective_handicap))
    return result


def season_matchups_ready(session: Session, season_id: int) -> bool:
    counts = session.execute(
        select(func.count(TeamMember.id)).select_from(Team).outerjoin(
            TeamMember, TeamMember.team_id == Team.id
        ).where(Team.season_id == season_id).group_by(Team.id)
    ).scalars()
    return season_ready_for_matchups(counts)


def assign_positions_by_handicap(session: Session, team_id: int) -> Team | None:
    """Apply positions by effective seed, with golfer ID as the stable tie-break."""
    team = session.get(Team, team_id)
    if team is None:
        return None
    current = list(session.execute(
        select(TeamMember).where(TeamMember.team_id == team_id).order_by(TeamMember.position)
    ).scalars())
    if not current:
        raise TeamValidationError({"members": "A team needs at least one member before positions can be assigned."})
    seeded: list[tuple[int, int]] = []
    for member in current:
        golfer = session.get(Golfer, member.golfer_id)
        if golfer is None or not golfer.is_active:
            raise TeamValidationError({"members": "Every team member must be an active golfer."})
        try:
            effective = effective_participant(session, team.season_id, member.golfer_id)
        except ParticipantValidationError:
            effective = None
        season = session.get(Season, team.season_id)
        if effective is None or season is None or effective.effective_tee.course_id != season.course_id:
            raise TeamValidationError({"members": "Every team member needs an effective tee at this season's course."})
        if effective is None or effective.effective_handicap is None:
            raise TeamValidationError({"members": "Every team member needs an effective seed handicap before positions can be assigned."})
        seeded.append((member.golfer_id, effective.effective_handicap))
    ordered_ids = order_members_by_seed(seeded)
    for member in current:
        session.delete(member)
    session.flush()
    session.add_all(
        TeamMember(team_id=team.id, season_id=team.season_id, golfer_id=golfer_id, position=position)
        for position, golfer_id in enumerate(ordered_ids, start=1)
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise TeamValidationError({"members": "Positions could not be assigned."}) from None
    session.refresh(team)
    return team


__all__ = [
    "TeamValidationError", "TeamView", "save_team", "get_team",
    "list_season_teams", "list_assignable_golfers", "season_matchups_ready",
    "assign_positions_by_handicap",
]
