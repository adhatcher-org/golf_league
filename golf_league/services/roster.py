"""Session-taking CRUD operations for the roster.

Routes call these; they never touch `golf_league.models` or run SQL
themselves. Pure validation and normalization live in
`golf_league.domain.roster` and are applied here before anything is
written.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.roster import (
    derive_handicap_status,
    normalize_email,
    normalize_name,
    normalize_phone,
    validate_handicap_strokes,
)
from golf_league.models import (
    Golfer,
    Season,
    SeasonGolfer,
    SeasonParticipant,
    Team,
    TeamMember,
    TeeSet,
    User,
)

# Sentinel distinguishing "the caller did not pass `handicap_strokes` at
# all" from "the caller passed an explicit blank meaning NULL" (`""`) or a
# real value. Only `update_golfer` uses it: changing `default_tee_set_id`
# without supplying `handicap_strokes` again is a validation error, but an
# update that leaves the tee alone may omit it to keep the stored value.
_UNSET = object()


class RosterValidationError(Exception):
    """Raised when input fails validation; carries field -> message errors."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        self.status_code = 422
        super().__init__(str(errors))


class RosterConflictError(RosterValidationError):
    """A valid golfer edit refused because it conflicts with active membership."""

    def __init__(self, errors: dict[str, str]):
        super().__init__(errors)
        self.status_code = 409


def _parse_handicap_strokes(value: object) -> int | None:
    """Convert an already-validated handicap value to `int | None`.

    Callers must call `validate_handicap_strokes` first; this never raises
    for input that passed validation.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return None
    return int(text)


def _validate_golfer_fields(
    session: Session,
    *,
    first_name: str,
    last_name: str,
    default_tee_set_id: object,
    default_tee_label: object = None,
    handicap_strokes: object,
) -> dict[str, str]:
    errors: dict[str, str] = {}

    if not first_name or not normalize_name(first_name):
        errors["first_name"] = "First name is required."
    if not last_name or not normalize_name(last_name):
        errors["last_name"] = "Last name is required."

    if default_tee_label is not None:
        if str(default_tee_label).strip() not in {"Blue", "White", "Gold"}:
            errors["default_tee_label"] = "Choose Blue, White, or Gold."
    elif default_tee_set_id is None or not str(default_tee_set_id).strip():
        errors["default_tee_set_id"] = "Tee is required."
    else:
        try:
            tee_set_id = int(str(default_tee_set_id).strip())
        except ValueError:
            errors["default_tee_set_id"] = "Unknown tee set."
        else:
            tee_set = session.get(TeeSet, tee_set_id)
            if tee_set is None:
                errors["default_tee_set_id"] = "Unknown tee set."

    handicap_error = validate_handicap_strokes(handicap_strokes)
    if handicap_error:
        errors["handicap_strokes"] = handicap_error

    return errors


def _parse_active_value(value: object, current: bool) -> bool:
    text = str(value).strip().lower()
    if not text:
        return current
    if text in {"true", "1", "on"}:
        return True
    if text in {"false", "0", "off"}:
        return False
    raise RosterValidationError({"is_active": "Choose active or inactive."})


def _active_team_name(session: Session, golfer_id: int) -> str | None:
    return session.execute(
        select(Team.name)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .join(Season, Season.id == TeamMember.season_id)
        .where(TeamMember.golfer_id == golfer_id, Season.status == "active")
        .order_by(Team.name, Team.id)
        .limit(1)
    ).scalar_one_or_none()


def create_golfer(
    session: Session,
    *,
    first_name: str,
    last_name: str,
    default_tee_set_id: object,
    email: str | None = None,
    phone: str | None = None,
    handicap_strokes: object = None,
    notes: str | None = None,
) -> Golfer:
    """Create one golfer, or raise `RosterValidationError` (writes nothing)."""
    errors = _validate_golfer_fields(
        session,
        first_name=first_name,
        last_name=last_name,
        default_tee_set_id=default_tee_set_id,
        handicap_strokes=handicap_strokes,
    )
    if errors:
        raise RosterValidationError(errors)

    normalized_email = normalize_email(email)
    strokes = _parse_handicap_strokes(handicap_strokes)
    tee_set = session.get(TeeSet, int(str(default_tee_set_id).strip()))

    golfer = Golfer(
        first_name=normalize_name(first_name),
        last_name=normalize_name(last_name),
        email=normalized_email,
        phone=normalize_phone(phone),
        default_tee_set_id=int(str(default_tee_set_id).strip()),
        default_tee_label=tee_set.color_label,
        handicap_strokes=strokes,
        handicap_source="self_reported",
        handicap_status=derive_handicap_status(
            email=normalized_email, handicap_strokes=strokes
        ),
        notes=notes.strip() if notes and notes.strip() else None,
    )
    session.add(golfer)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise RosterValidationError(
            {"email": "A golfer with this email already exists."}
        ) from None
    session.refresh(golfer)
    return golfer


def _tee_update_values(
    session: Session,
    golfer: Golfer,
    *,
    default_tee_set_id: object,
    default_tee_label: object,
    errors: dict[str, str],
) -> tuple[int | None, str | None, bool]:
    if default_tee_label is not None:
        if "default_tee_label" in errors:
            return None, None, False
        label = str(default_tee_label).strip()
        return None, label, label != golfer.default_tee_label
    if "default_tee_set_id" in errors:
        return None, None, False
    tee_set_id = int(str(default_tee_set_id).strip())
    tee_set = session.get(TeeSet, tee_set_id)
    return tee_set_id, tee_set.color_label, tee_set_id != golfer.default_tee_set_id


def update_golfer(
    session: Session,
    golfer_id: int,
    *,
    first_name: str,
    last_name: str,
    default_tee_set_id: object = None,
    default_tee_label: object = None,
    email: str | None = None,
    phone: str | None = None,
    handicap_strokes: object = _UNSET,
    notes: str | None = None,
    is_active: object = None,
) -> Golfer | None:
    """Update a golfer; returns None when `golfer_id` does not exist.

    When the tee label or tee set changes, `handicap_strokes` must be
    supplied again in this same call — either a number or an explicit
    blank (`""`) meaning NULL.
    Leaving `handicap_strokes` at its `_UNSET` default while changing the
    tee is a validation error naming `handicap_strokes`; the previously
    stored strokes are never carried across a tee change. Omitting it when
    the tee is unchanged simply keeps the stored value.
    """
    golfer = session.get(Golfer, golfer_id)
    if golfer is None:
        return None

    supplied = handicap_strokes is not _UNSET
    errors = _validate_golfer_fields(
        session,
        first_name=first_name,
        last_name=last_name,
        default_tee_set_id=(None if default_tee_label is not None else default_tee_set_id),
        default_tee_label=default_tee_label,
        handicap_strokes=(handicap_strokes if supplied else None),
    )

    new_tee_set_id, new_tee_label, tee_changed = _tee_update_values(
        session,
        golfer,
        default_tee_set_id=default_tee_set_id,
        default_tee_label=default_tee_label,
        errors=errors,
    )
    if tee_changed:
        if tee_changed and not supplied:
            errors["handicap_strokes"] = (
                "Changing the tee requires the handicap to be supplied again."
            )

    if errors:
        raise RosterValidationError(errors)

    active_value = _parse_active_value(is_active, golfer.is_active) if is_active is not None else golfer.is_active
    if golfer.is_active and not active_value:
        active_membership = _active_team_name(session, golfer_id)
        if active_membership is not None:
            raise RosterConflictError(
                {"is_active": f"Cannot deactivate a member of active-season team {active_membership}."}
            )

    normalized_email = normalize_email(email)
    strokes = (
        _parse_handicap_strokes(handicap_strokes)
        if supplied
        else golfer.handicap_strokes
    )

    golfer.first_name = normalize_name(first_name)
    golfer.last_name = normalize_name(last_name)
    golfer.email = normalized_email
    golfer.phone = normalize_phone(phone)
    golfer.default_tee_set_id = new_tee_set_id
    golfer.default_tee_label = new_tee_label
    golfer.handicap_strokes = strokes
    golfer.handicap_status = derive_handicap_status(
        email=normalized_email, handicap_strokes=strokes
    )
    golfer.notes = notes.strip() if notes and notes.strip() else None
    golfer.is_active = active_value

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise RosterValidationError(
            {"email": "A golfer with this email already exists."}
        ) from None
    session.refresh(golfer)
    return golfer


def get_golfer(session: Session, golfer_id: int) -> Golfer | None:
    """Return the `Golfer` with `golfer_id`, or None."""
    return session.get(Golfer, golfer_id)


def list_golfers(session: Session) -> list[Golfer]:
    """Return every golfer, ordered by last name then first name."""
    return list(
        session.execute(
            select(Golfer).order_by(Golfer.last_name, Golfer.first_name)
        )
        .scalars()
        .all()
    )


def delete_golfer(session: Session, golfer_id: int) -> bool | None:
    """Delete an unreferenced golfer; return None when the id is absent."""
    golfer = session.get(Golfer, golfer_id)
    if golfer is None:
        return None
    user_count = session.query(User).filter_by(golfer_id=golfer_id).count()
    participant_count = session.query(SeasonParticipant).filter_by(golfer_id=golfer_id).count()
    season_roster_count = session.query(SeasonGolfer).filter_by(golfer_id=golfer_id).count()
    team_membership_count = session.query(TeamMember).filter_by(golfer_id=golfer_id).count()
    if user_count or participant_count or season_roster_count or team_membership_count:
        references = []
        if user_count:
            references.append(f"linked to {user_count} user account{'s' if user_count != 1 else ''}")
        if participant_count:
            references.append(f"on {participant_count} season participant row{'s' if participant_count != 1 else ''}")
        if season_roster_count:
            references.append(f"included in {season_roster_count} season roster{'s' if season_roster_count != 1 else ''}")
        if team_membership_count:
            references.append(f"on {team_membership_count} team membership row{'s' if team_membership_count != 1 else ''}")
        raise RosterValidationError({"delete": "Cannot delete golfer: " + ", ".join(references) + "."})
    session.delete(golfer)
    session.commit()
    return True


@dataclass(frozen=True)
class RosterPlayer:
    """The privacy-safe projection used by the verified player roster."""

    first_name: str
    last_name: str
    tee_color: str
    handicap_strokes: int | None


def list_active_roster_players(session: Session) -> list[RosterPlayer]:
    """Load only columns permitted in the verified player-roster view."""
    rows = session.execute(
        select(
            Golfer.first_name,
            Golfer.last_name,
            Golfer.default_tee_label,
            Golfer.handicap_strokes,
        )
        .where(Golfer.is_active.is_(True))
        .order_by(Golfer.last_name, Golfer.first_name)
    ).all()
    return [RosterPlayer(*row) for row in rows]


__all__ = [
    "RosterValidationError",
    "RosterConflictError",
    "create_golfer",
    "update_golfer",
    "get_golfer",
    "list_golfers",
    "delete_golfer",
    "RosterPlayer",
    "list_active_roster_players",
]
