"""Session-taking schedule operations for seasons and printed weeks."""

import hashlib
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from golf_league.domain.schedule import generate_weeks, rotate_nines, shift_from
from golf_league.models import Season, SeasonGolfer, TeamMatch, TeeRating, Week
from golf_league.services.participants import (
    ParticipantValidationError,
    effective_participant,
)
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
        raise ScheduleValidationError({"week_type": "This rain date has no nine; use the separate GL-37 activation workflow."})
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
    match_count = session.scalar(select(func.count(TeamMatch.id)).where(TeamMatch.week_id == week_id))
    if match_count:
        raise ScheduleConflict(f"Cannot delete week: referenced by {match_count} team match(es).")
    if refs:
        raise ScheduleConflict("This week is referenced as a makeup target by another week.")
    session.delete(week)
    session.commit()


__all__ = [
    "ScheduleConflict", "ScheduleValidationError", "WeekProposal", "build_default_proposal",
    "commit_generated_weeks", "delete_week", "edit_week", "list_weeks", "schedule_fingerprint",
    "validate_proposal", "week_fingerprint",
]


@dataclass(frozen=True)
class WeekChange:
    week_id: int
    index: int
    old_value: date | str
    new_value: date | str


@dataclass(frozen=True)
class ParWarning:
    week_id: int
    index: int
    golfer_id: int
    golfer_name: str
    tee_set_id: int
    tee_name: str
    old_par: int
    new_par: int


@dataclass(frozen=True)
class SchedulePreview:
    changes: tuple[WeekChange, ...]
    warnings: tuple[ParWarning, ...]
    fingerprint: str


def reserve_schedule_write(session: Session) -> None:
    """Reserve SQLite writes before fresh validation; never commit the caller."""
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        raise ScheduleConflict("Schedule confirmation requires SQLite write serialization.")
    try:
        if connection.connection.driver_connection.in_transaction:
            # A caller may already own a transaction. Upgrade it before any reads.
            connection.exec_driver_sql("UPDATE seasons SET id = id WHERE 0")
        else:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    except OperationalError as exc:
        raise ScheduleConflict("The schedule is being changed. Retry a fresh preview.") from exc


def _preview_state(session: Session, season_id: int, week_id: int):
    with session.no_autoflush:
        season = session.get(Season, season_id, populate_existing=True)
        weeks = list(session.scalars(select(Week).where(Week.season_id == season_id)
                                    .order_by(Week.index).execution_options(populate_existing=True)))
    week = next((row for row in weeks if row.id == week_id), None)
    if season is None or week is None:
        raise LookupError("Week or season not found.")
    return season, weeks, week


def _intent_fingerprint(season, weeks, week_id, action, target, rerotate=False):
    raw = "|".join([schedule_fingerprint(season), *(week_fingerprint(row) for row in weeks),
                    str(season.course_id), str(season.year), str(season.name_override), str(season.status),
                    str(week_id), action, str(target), str(rerotate)])
    return hashlib.sha256(raw.encode()).hexdigest()


def preview_shift(session: Session, season_id: int, week_id: int, *, new_date: date) -> SchedulePreview:
    season, weeks, week = _preview_state(session, season_id, week_id)
    if any(row.status == "played" for row in weeks if row.index >= week.index):
        raise ScheduleConflict("Played weeks cannot be shifted.")
    try:
        shifted = shift_from([(row.index, row.play_date, row.status) for row in weeks], week.index, new_date)
    except ValueError as exc:
        raise ScheduleValidationError({"new_date": str(exc)}) from exc
    earlier_dates = {row.play_date for row in weeks if row.index < week.index}
    if any(new in earlier_dates for _, _, new in shifted):
        raise ScheduleConflict("A shifted date collides with an earlier week.")
    if shifted[-1][2] < season.start_date:
        raise ScheduleValidationError({"new_date": "The season end cannot precede its start."})
    by_index = {row.index: row.id for row in weeks}
    changes = tuple(WeekChange(by_index[index], index, old, new) for index, old, new in shifted if old != new)
    return SchedulePreview(changes, (), _intent_fingerprint(season, weeks, week_id, "shift", new_date))


def _par_warnings(session: Session, season_id: int, changes) -> tuple[ParWarning, ...]:
    warnings = []
    roster_ids = list(session.scalars(select(SeasonGolfer.golfer_id)
                                     .where(SeasonGolfer.season_id == season_id).order_by(SeasonGolfer.golfer_id)))
    for golfer_id in roster_ids:
        try:
            view = effective_participant(session, season_id, golfer_id)
        except ParticipantValidationError:
            continue  # No effective tee: do not invent a numeric par comparison.
        if view is None:
            continue
        tee = view.effective_tee
        pars = {row.scope: row.par for row in session.scalars(select(TeeRating).where(TeeRating.tee_set_id == tee.id))}
        for change in changes:
            old, new = pars.get(change.old_value), pars.get(change.new_value)
            if old is not None and new is not None and old != new:
                golfer = view.golfer
                warnings.append(ParWarning(change.week_id, change.index, golfer_id,
                                           f"{golfer.first_name} {golfer.last_name}", tee.id,
                                           tee.name, old, new))
    return tuple(warnings)


def preview_nine(session: Session, season_id: int, week_id: int, *, new_nine: str,
                 rerotate: bool = False) -> SchedulePreview:
    season, weeks, week = _preview_state(session, season_id, week_id)
    if week.status == "played":
        raise ScheduleConflict("Played weeks cannot be changed.")
    if week.status != "scheduled" or week.nine is None:
        raise ScheduleValidationError({"new_nine": "Choose a scheduled week with a nine. Reserved rain dates require GL-37 activation."})
    eligible = [row for row in weeks if row.status == "scheduled"]
    try:
        rotated = rotate_nines([(row.index, row.nine) for row in eligible], week.index, new_nine)
    except ValueError as exc:
        raise ScheduleValidationError({"new_nine": str(exc)}) from exc
    by_index = {row.index: row for row in weeks}
    changes = tuple(WeekChange(by_index[index].id, index, by_index[index].nine, nine)
                    for index, nine in rotated if nine is not None and by_index[index].nine != nine
                    and (rerotate or index == week.index))
    with session.no_autoflush:
        warnings = _par_warnings(session, season_id, changes)
    return SchedulePreview(changes, warnings, _intent_fingerprint(season, weeks, week_id, "nine", new_nine, rerotate))


def confirm_nine_core(session: Session, season_id: int, week_id: int, *, new_nine: str,
                      rerotate: bool = False, expected_fingerprint: str) -> SchedulePreview:
    """Apply only week metadata inside the caller's transaction, without committing.

    GL-36 can relabel snapshots for the returned changes before its single commit.
    The core reserves writes and reads fresh state even when the caller cached rows.
    """
    reserve_schedule_write(session)
    preview = preview_nine(session, season_id, week_id, new_nine=new_nine, rerotate=rerotate)
    if preview.fingerprint != expected_fingerprint:
        raise ScheduleConflict("The schedule or requested change changed after this preview. Review a fresh preview.")
    for change in preview.changes:
        session.get(Week, change.week_id).nine = change.new_value
    return preview


def confirm_nine(session: Session, season_id: int, week_id: int, *, new_nine: str,
                 rerotate: bool = False, expected_fingerprint: str) -> SchedulePreview:
    try:
        preview = confirm_nine_core(session, season_id, week_id, new_nine=new_nine,
                                    rerotate=rerotate, expected_fingerprint=expected_fingerprint)
        session.commit()
        return preview
    except Exception:
        session.rollback()
        raise


def confirm_shift(session: Session, season_id: int, week_id: int, *, new_date: date,
                  expected_fingerprint: str) -> SchedulePreview:
    try:
        reserve_schedule_write(session)
        preview = preview_shift(session, season_id, week_id, new_date=new_date)
        if preview.fingerprint != expected_fingerprint:
            raise ScheduleConflict("The schedule or requested change changed after this preview. Review a fresh preview.")
        for change in preview.changes:
            session.get(Week, change.week_id).play_date = change.new_value
        if preview.changes:
            last = list_weeks(session, season_id)[-1]
            session.get(Season, season_id).end_date = last.play_date
        session.commit()
        return preview
    except Exception:
        session.rollback()
        raise
