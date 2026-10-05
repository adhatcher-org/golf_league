"""Session-taking schedule operations for seasons and printed weeks."""

import hashlib
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.schedule import generate_weeks
from golf_league.models import Season, Week
from golf_league.services.seasons import season_week_count

WEEK_TYPES = ("match", "play_with_team", "rain_date")
WEEK_STATUSES = ("scheduled", "cancelled", "played")


class ScheduleValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


class ScheduleConflict(Exception):
    """A stale preview or a schedule that already exists."""


@dataclass(frozen=True)
class WeekProposal:
    play_date: date
    nine: str | None
    week_type: str
    status: str
    makeup_for_index: int | None = None
    notes: str | None = None


def schedule_fingerprint(season: Season) -> str:
    raw = "|".join(map(str, (season.id, season.start_date.isoformat(), season.end_date.isoformat(), season.play_weekday, season.first_week_nine)))
    return hashlib.sha256(raw.encode()).hexdigest()


def list_weeks(session: Session, season_id: int) -> list[Week]:
    return list(session.execute(
        select(Week).where(Week.season_id == season_id).order_by(Week.index)
    ).scalars())


def build_default_proposal(season: Season, count: object) -> list[WeekProposal]:
    errors: dict[str, str] = {}
    try:
        if isinstance(count, bool) or not str(count).strip().isdecimal():
            raise ValueError
        parsed_count = int(str(count).strip())
    except (TypeError, ValueError):
        raise ScheduleValidationError({"count": "Week count must be a positive whole number."}) from None
    expected = season_week_count(season)
    if parsed_count != expected:
        errors["count"] = f"Week count must match the season dates ({expected})."
    if season.start_date.weekday() != season.play_weekday:
        errors["start_date"] = "Start date must fall on the season weekday."
    if errors:
        raise ScheduleValidationError(errors)
    try:
        generated = generate_weeks(season.start_date, season.play_weekday, parsed_count, season.first_week_nine)
    except ValueError as exc:
        raise ScheduleValidationError({"start_date": str(exc)}) from exc
    return [WeekProposal(play_date, nine, "match", "scheduled") for play_date, nine in generated]


def validate_proposal(proposal: list[WeekProposal], expected_count: int) -> None:  # noqa: C901
    errors: dict[str, str] = {}
    if len(proposal) != expected_count:
        errors["count"] = "The submitted rows do not match the season dates."
    by_index = dict(enumerate(proposal, 1))
    for index, row in by_index.items():
        if row.week_type not in WEEK_TYPES:
            errors[f"week_type_{index}"] = "Choose a valid week type."
        elif row.week_type == "rain_date":
            if row.nine is not None:
                errors[f"nine_{index}"] = "Rain-date weeks must have no nine until activated."
        elif row.nine not in ("front", "back"):
            errors[f"nine_{index}"] = "Match and play-with-team weeks need a front or back nine."
        if row.status not in WEEK_STATUSES:
            errors[f"status_{index}"] = "Choose a valid week status."
        elif row.status == "played":
            errors[f"status_{index}"] = "New weeks cannot be marked played during generation."
        target = row.makeup_for_index
        if target is not None and (target == index or target not in by_index):
            errors[f"makeup_{index}"] = "Makeup must point to another week in this season."
    if not errors:
        for origin in by_index:
            seen: set[int] = set()
            current = origin
            while by_index[current].makeup_for_index is not None:
                current = by_index[current].makeup_for_index  # type: ignore[assignment]
                if current in seen or current == origin:
                    errors[f"makeup_{origin}"] = "Makeup links cannot form a cycle."
                    break
                seen.add(current)
    if errors:
        raise ScheduleValidationError(errors)


def commit_generated_weeks(  # noqa: C901
    session: Session,
    season_id: int,
    *,
    proposal: list[WeekProposal],
    expected_fingerprint: str,
) -> list[Week]:
    season = session.get(Season, season_id)
    if season is None:
        raise LookupError("Season not found.")
    if schedule_fingerprint(season) != expected_fingerprint:
        raise ScheduleConflict("The season changed after this preview. Review a fresh preview.")
    if list_weeks(session, season_id):
        raise ScheduleConflict("This season already has weeks; schedule generation is blocked.")
    validate_proposal(proposal, season_week_count(season))
    expected_rows = build_default_proposal(season, season_week_count(season))
    for index, (row, expected) in enumerate(zip(proposal, expected_rows, strict=True), 1):
        if row.play_date != expected.play_date:
            raise ScheduleValidationError({f"play_date_{index}": "Week dates must follow the season schedule."})
        if row.week_type != "rain_date" and row.nine != expected.nine:
            raise ScheduleValidationError({f"nine_{index}": "Nines must alternate from the season's first-week nine."})
    by_index = dict(enumerate(proposal, 1))
    rows = [Week(
        season_id=season_id,
        index=index,
        play_date=item.play_date,
        nine=item.nine,
        week_type=item.week_type,
        status=item.status,
        notes=item.notes or None,
    ) for index, item in by_index.items()]
    session.add_all(rows)
    try:
        session.flush()
        for row, item in zip(rows, proposal, strict=True):
            if item.makeup_for_index is not None:
                row.makeup_for_week_id = rows[item.makeup_for_index - 1].id
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ScheduleConflict("This season already has weeks; schedule generation is blocked.") from exc
    for row in rows:
        session.refresh(row)
    return rows


def week_fingerprint(week: Week) -> str:
    raw = "|".join(map(str, (week.id, week.season_id, week.index, week.play_date.isoformat(), week.nine, week.week_type, week.status, week.makeup_for_week_id, week.notes)))
    return hashlib.sha256(raw.encode()).hexdigest()


def edit_week(  # noqa: C901
    session: Session,
    season_id: int,
    week_id: int,
    *,
    week_type: str,
    status: str,
    makeup_for_index: object,
    notes: str,
    expected_fingerprint: str,
) -> Week:
    week = session.get(Week, week_id)
    if week is None or week.season_id != season_id:
        raise LookupError("Week not found.")
    if week_fingerprint(week) != expected_fingerprint:
        raise ScheduleConflict("This week changed after the form was opened. Reload and try again.")
    if week_type not in WEEK_TYPES:
        raise ScheduleValidationError({"week_type": "Choose a valid week type."})
    if status not in WEEK_STATUSES:
        raise ScheduleValidationError({"status": "Choose a valid week status."})
    if status == "played" and week.status != "played":
        raise ScheduleValidationError({"status": "Weeks cannot be marked played in this MVP."})
    if week_type == "rain_date" and week.nine is not None:
        raise ScheduleValidationError({"week_type": "A week with a nine cannot be changed to a rain date."})
    if week_type != "rain_date" and week.nine is None:
        raise ScheduleValidationError({"week_type": "This rain date has no nine; nine editing is available in GL-34."})
    if week.status == "played" and (week_type != week.week_type or status != week.status):
        raise ScheduleConflict("Played weeks cannot be changed.")
    if status == "cancelled" and week.week_type != "match":
        raise ScheduleValidationError({"status": "Only match weeks can be cancelled here."})
    target_id = None
    if str(makeup_for_index).strip():
        text = str(makeup_for_index).strip()
        if not text.isdecimal():
            raise ScheduleValidationError({"makeup_for_index": "Choose a valid makeup target."})
        target_index = int(text)
        target = session.execute(select(Week).where(Week.season_id == season_id, Week.index == target_index)).scalar_one_or_none()
        if target is None or target.id == week.id:
            raise ScheduleValidationError({"makeup_for_index": "Makeup must point to another week in this season."})
        target_id = target.id
        current = target
        seen = {week.id}
        while True:
            if current.id in seen:
                raise ScheduleValidationError({"makeup_for_index": "Makeup links cannot form a cycle."})
            seen.add(current.id)
            if current.makeup_for_week_id is None:
                break
            current = session.get(Week, current.makeup_for_week_id)
    week.week_type = week_type
    week.status = status
    week.makeup_for_week_id = target_id
    week.notes = notes.strip() or None
    session.commit()
    session.refresh(week)
    return week


def delete_week(session: Session, season_id: int, week_id: int) -> None:
    week = session.get(Week, week_id)
    if week is None or week.season_id != season_id:
        raise LookupError("Week not found.")
    refs = session.execute(select(Week.id).where(Week.makeup_for_week_id == week_id).limit(1)).first()
    if refs:
        raise ScheduleConflict("This week is referenced as a makeup target by another week.")
    session.delete(week)
    session.commit()


__all__ = [
    "ScheduleConflict", "ScheduleValidationError", "WeekProposal", "build_default_proposal",
    "commit_generated_weeks", "delete_week", "edit_week", "list_weeks", "schedule_fingerprint",
    "validate_proposal", "week_fingerprint",
]
