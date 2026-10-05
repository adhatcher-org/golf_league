"""Season-scoped participant override operations."""

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.roster import validate_handicap_strokes
from golf_league.models import Golfer, Season, SeasonParticipant, TeeSet


class ParticipantValidationError(Exception):
    """Invalid season participant input, with field-level messages."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


@dataclass(frozen=True)
class ParticipantView:
    participant: SeasonParticipant | None
    golfer: Golfer
    effective_tee: TeeSet
    effective_handicap: int | None


def _whole_id(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ParticipantValidationError({field: "Choose a valid golfer or tee."})
    text = str(value).strip()
    if not text or not text.isdecimal():
        raise ParticipantValidationError({field: "Choose a valid golfer or tee."})
    return int(text)


def _parse_handicap(value: object) -> int | None:
    error = validate_handicap_strokes(value)
    if error:
        raise ParticipantValidationError({"seed_handicap_strokes": error})
    if value is None or not str(value).strip():
        return None
    return int(str(value).strip())


def _validate_seed_source(value: object) -> str | None:
    source = str(value or "").strip()
    if len(source) > 120:
        raise ParticipantValidationError({"seed_source": "Source must be 120 characters or fewer."})
    return source or None


def _default_tee(session: Session, season: Season, golfer: Golfer) -> TeeSet | None:
    if golfer.default_tee_set_id is not None:
        tee = session.get(TeeSet, golfer.default_tee_set_id)
        if tee is not None and tee.course_id == season.course_id:
            return tee
    return session.execute(select(TeeSet).where(
        TeeSet.course_id == season.course_id,
        TeeSet.color_label == golfer.default_tee_label,
    ).order_by(TeeSet.id).limit(1)).scalar_one_or_none()


def _effective_values(session: Session, season: Season, golfer: Golfer,
                      participant: SeasonParticipant | None) -> tuple[TeeSet, int | None]:
    if participant and participant.tee_set_id is not None:
        effective_tee = participant.tee_set
    else:
        effective_tee = _default_tee(session, season, golfer)
    if effective_tee is None:
        raise ParticipantValidationError({"golfer_id": "Golfer's tee does not match a tee at this season's course."})
    if participant and participant.seed_handicap_strokes is not None:
        handicap = participant.seed_handicap_strokes
    elif participant is None or participant.tee_set_id is None:
        handicap = golfer.handicap_strokes
    else:
        handicap = None
    return effective_tee, handicap


def get_season(session: Session, season_id: int) -> Season | None:
    return session.get(Season, season_id)


def get_participant(session: Session, participant_id: int) -> SeasonParticipant | None:
    return session.get(SeasonParticipant, participant_id)


def list_course_tees(session: Session, season_id: int) -> list[TeeSet]:
    season = session.get(Season, season_id)
    if season is None:
        return []
    return list(session.execute(select(TeeSet).where(TeeSet.course_id == season.course_id).order_by(TeeSet.sort_order, TeeSet.id)).scalars())


def list_participant_views(session: Session, season_id: int) -> list[ParticipantView]:
    if session.get(Season, season_id) is None:
        return []
    rows = session.execute(
        select(SeasonParticipant, Golfer)
        .join(Golfer, Golfer.id == SeasonParticipant.golfer_id)
        .where(SeasonParticipant.season_id == season_id)
        .order_by(Golfer.last_name, Golfer.first_name)
    ).all()
    season = session.get(Season, season_id)
    return [ParticipantView(row, golfer, *_effective_values(session, season, golfer, row)) for row, golfer in rows]


def list_available_golfers(session: Session, season_id: int) -> list[Golfer]:
    season = session.get(Season, season_id)
    if season is None:
        return []
    participant_ids = select(SeasonParticipant.golfer_id).where(SeasonParticipant.season_id == season_id)
    course_tee_ids = select(TeeSet.id).where(TeeSet.course_id == season.course_id)
    course_tee_labels = select(TeeSet.color_label).where(TeeSet.course_id == season.course_id)
    return list(session.execute(
        select(Golfer).where(
            Golfer.is_active.is_(True),
            or_(
                Golfer.default_tee_set_id.in_(course_tee_ids),
                Golfer.default_tee_label.in_(course_tee_labels),
            ),
            Golfer.id.not_in(participant_ids),
        )
        .order_by(Golfer.last_name, Golfer.first_name)
    ).scalars())


def _checked_golfer(session: Session, season: Season, golfer_id: object) -> Golfer:
    golfer = session.get(Golfer, _whole_id(golfer_id, "golfer_id"))
    if golfer is None or not golfer.is_active:
        raise ParticipantValidationError({"golfer_id": "Choose an active golfer."})
    default_tee = _default_tee(session, season, golfer)
    if default_tee is None:
        raise ParticipantValidationError({"golfer_id": "Golfer's default tee must belong to this season's course."})
    return golfer


def _checked_tee(session: Session, season: Season, tee_id: object) -> int:
    parsed_id = _whole_id(tee_id, "tee_set_id")
    tee = session.get(TeeSet, parsed_id)
    if tee is None or tee.course_id != season.course_id:
        raise ParticipantValidationError({"tee_set_id": "Choose a tee at this season's course."})
    return parsed_id


def add_participant_override(session: Session, season_id: int, *, golfer_id: object,
                             tee_set_id: object = "", seed_handicap_strokes: object = "",
                             seed_source: object = "") -> SeasonParticipant | None:
    season = session.get(Season, season_id)
    if season is None:
        return None
    golfer = _checked_golfer(session, season, golfer_id)
    tee_id = _checked_tee(session, season, tee_set_id) if str(tee_set_id).strip() else None
    strokes = _parse_handicap(seed_handicap_strokes)
    source = _validate_seed_source(seed_source)
    if source and strokes is None:
        raise ParticipantValidationError({"seed_source": "Enter a seed handicap before recording its source."})
    if tee_id is None and strokes is None:
        raise ParticipantValidationError({"override": "Enter a tee or seed override; golfers without overrides need no participant row."})
    row = SeasonParticipant(season_id=season_id, golfer_id=golfer.id, tee_set_id=tee_id,
                            seed_handicap_strokes=strokes, seed_source=source)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ParticipantValidationError({"golfer_id": "This golfer already has an override row for the season."}) from None
    session.refresh(row)
    return row


def _apply_tee_override(session: Session, season: Season, row: SeasonParticipant,
                        action: str, tee_set_id: object) -> None:
    if action == "set":
        row.tee_set_id = _checked_tee(session, season, tee_set_id)
    elif action == "clear":
        row.tee_set_id = None


def _apply_seed_override(row: SeasonParticipant, action: str,
                         seed_handicap_strokes: object, seed_source: object) -> None:
    if action == "set":
        parsed_seed = _parse_handicap(seed_handicap_strokes)
        if parsed_seed is None:
            raise ParticipantValidationError({"seed_handicap_strokes": "Enter a number, including 0, or choose clear to fall back."})
        row.seed_handicap_strokes = parsed_seed
        row.seed_source = _validate_seed_source(seed_source)
    elif action == "clear":
        row.seed_handicap_strokes = None
        row.seed_source = None


def update_participant_override(session: Session, season_id: int, participant_id: int, *,
                                tee_action: str, tee_set_id: object, seed_action: str,
                                seed_handicap_strokes: object, seed_source: object) -> SeasonParticipant | None:
    season = session.get(Season, season_id)
    row = session.get(SeasonParticipant, participant_id)
    if season is None or row is None or row.season_id != season_id:
        return None
    _checked_golfer(session, season, row.golfer_id)
    if tee_action not in {"keep", "set", "clear"} or seed_action not in {"keep", "set", "clear"}:
        raise ParticipantValidationError({"override": "Choose whether each value stays, changes, or falls back."})
    _apply_tee_override(session, season, row, tee_action, tee_set_id)
    _apply_seed_override(row, seed_action, seed_handicap_strokes, seed_source)
    if row.tee_set_id is None and row.seed_handicap_strokes is None and row.seed_source is None:
        session.delete(row)
        session.commit()
        return None
    # A different effective tee without a seed is saved as missing; later
    # matchup placement must refuse it rather than carrying the roster seed.
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ParticipantValidationError({"override": "Override could not be saved."}) from None
    session.refresh(row)
    return row


def effective_participant(session: Session, season_id: int, golfer_id: int) -> ParticipantView | None:
    season = session.get(Season, season_id)
    golfer = session.get(Golfer, golfer_id)
    if season is None or golfer is None:
        return None
    default_tee = _default_tee(session, season, golfer)
    if default_tee is None:
        raise ParticipantValidationError({"golfer_id": "Golfer's default tee does not belong to the season course."})
    row = session.execute(select(SeasonParticipant).where(
        SeasonParticipant.season_id == season_id, SeasonParticipant.golfer_id == golfer_id
    )).scalar_one_or_none()
    tee, handicap = _effective_values(session, season, golfer, row)
    return ParticipantView(row, golfer, tee, handicap)
