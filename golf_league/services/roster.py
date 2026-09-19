"""Session-taking CRUD operations for the roster.

Routes call these; they never touch `golf_league.models` or run SQL
themselves. Pure validation and normalization live in
`golf_league.domain.roster` and are applied here before anything is
written.
"""

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
from golf_league.models import Golfer, TeeSet

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
        super().__init__(str(errors))


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
    handicap_strokes: object,
) -> dict[str, str]:
    errors: dict[str, str] = {}

    if not first_name or not normalize_name(first_name):
        errors["first_name"] = "First name is required."
    if not last_name or not normalize_name(last_name):
        errors["last_name"] = "Last name is required."

    if default_tee_set_id is None or not str(default_tee_set_id).strip():
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

    golfer = Golfer(
        first_name=normalize_name(first_name),
        last_name=normalize_name(last_name),
        email=normalized_email,
        phone=normalize_phone(phone),
        default_tee_set_id=int(str(default_tee_set_id).strip()),
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


def update_golfer(
    session: Session,
    golfer_id: int,
    *,
    first_name: str,
    last_name: str,
    default_tee_set_id: object,
    email: str | None = None,
    phone: str | None = None,
    handicap_strokes: object = _UNSET,
    notes: str | None = None,
) -> Golfer | None:
    """Update a golfer; returns None when `golfer_id` does not exist.

    When `default_tee_set_id` names a different tee set than the one
    currently stored, `handicap_strokes` must be supplied again in this
    same call — either a number or an explicit blank (`""`) meaning NULL.
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
        default_tee_set_id=default_tee_set_id,
        handicap_strokes=(handicap_strokes if supplied else None),
    )

    new_tee_set_id: int | None = None
    if "default_tee_set_id" not in errors:
        new_tee_set_id = int(str(default_tee_set_id).strip())
        tee_changed = new_tee_set_id != golfer.default_tee_set_id
        if tee_changed and not supplied:
            errors["handicap_strokes"] = (
                "Changing the tee requires the handicap to be supplied again."
            )

    if errors:
        raise RosterValidationError(errors)

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
    golfer.handicap_strokes = strokes
    golfer.handicap_status = derive_handicap_status(
        email=normalized_email, handicap_strokes=strokes
    )
    golfer.notes = notes.strip() if notes and notes.strip() else None

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


__all__ = [
    "RosterValidationError",
    "create_golfer",
    "update_golfer",
    "get_golfer",
    "list_golfers",
]
