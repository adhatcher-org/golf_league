"""Season-scoped golfer inclusion."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import Golfer, Season, SeasonGolfer, TeamMember


class SeasonRosterValidationError(Exception):
    """Invalid season golfer selection."""


@dataclass(frozen=True)
class SeasonGolferView:
    golfer: Golfer
    included: bool
    assigned_to_team: bool


def list_season_golfers(session: Session, season_id: int) -> list[SeasonGolferView]:
    if session.get(Season, season_id) is None:
        return []
    included_ids = set(session.execute(
        select(SeasonGolfer.golfer_id).where(SeasonGolfer.season_id == season_id)
    ).scalars())
    assigned_ids = set(session.execute(
        select(TeamMember.golfer_id).where(TeamMember.season_id == season_id)
    ).scalars())
    golfers = session.execute(
        select(Golfer).order_by(Golfer.last_name, Golfer.first_name, Golfer.id)
    ).scalars()
    return [
        SeasonGolferView(golfer, golfer.id in included_ids, golfer.id in assigned_ids)
        for golfer in golfers
    ]


def save_season_golfers(session: Session, season_id: int, golfer_ids: list[object]) -> None:
    if session.get(Season, season_id) is None:
        raise SeasonRosterValidationError("Season not found.")

    try:
        selected_ids = {int(str(value).strip()) for value in golfer_ids if str(value).strip()}
    except ValueError as exc:
        raise SeasonRosterValidationError("The golfer selection is invalid.") from exc
    if any(golfer_id < 1 for golfer_id in selected_ids):
        raise SeasonRosterValidationError("The golfer selection is invalid.")

    assigned_ids = set(session.execute(
        select(TeamMember.golfer_id).where(TeamMember.season_id == season_id)
    ).scalars())
    # Team membership always implies season inclusion. Preserve assignments
    # when the roster form omits their disabled checkboxes.
    selected_ids.update(assigned_ids)
    active_ids = set(session.execute(
        select(Golfer.id).where(Golfer.id.in_(selected_ids), Golfer.is_active.is_(True))
    ).scalars()) if selected_ids else set()
    if selected_ids - assigned_ids != active_ids - assigned_ids:
        raise SeasonRosterValidationError("Only active golfers can be included in a season.")

    if not assigned_ids.issubset(selected_ids):
        raise SeasonRosterValidationError(
            "A golfer assigned to a team cannot be removed from the season roster."
        )

    existing_ids = set(session.execute(
        select(SeasonGolfer.golfer_id).where(SeasonGolfer.season_id == season_id)
    ).scalars())
    for golfer_id in existing_ids - selected_ids:
        row = session.execute(select(SeasonGolfer).where(
            SeasonGolfer.season_id == season_id,
            SeasonGolfer.golfer_id == golfer_id,
        )).scalar_one()
        session.delete(row)
    session.add_all(
        SeasonGolfer(season_id=season_id, golfer_id=golfer_id)
        for golfer_id in selected_ids - existing_ids
    )
    session.commit()


__all__ = [
    "SeasonGolferView",
    "SeasonRosterValidationError",
    "list_season_golfers",
    "save_season_golfers",
]
