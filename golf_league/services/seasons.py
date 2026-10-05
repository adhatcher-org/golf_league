"""Session-taking season configuration operations.

This module owns the structural season boundary.  It deliberately does not
create schedule rows or inspect future schedule tables.
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.config import Settings
from golf_league.domain.course import validate_hole_grid
from golf_league.models import (
    Course,
    Hole,
    HoleYardage,
    Season,
    SeasonParticipant,
    TeeRating,
    TeeSet,
    Week,
)

VALID_STATUSES = ("draft", "active", "complete")
VALID_FIRST_WEEK_NINES = ("front", "back")


class SeasonValidationError(Exception):
    """Raised for invalid season input, with a field-to-message mapping."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


def _parse_whole_number(
    value: object, field: str, minimum: int, maximum: int
) -> tuple[int | None, str | None]:
    """Parse only unsigned decimal digit input before converting to ``int``."""
    if isinstance(value, bool):
        return None, f"{field} must be a whole number."
    text = str(value).strip()
    if not text or not text.isdecimal():
        return None, f"{field} must be a whole number."
    parsed = int(text)
    if parsed < minimum or parsed > maximum:
        return None, f"{field} must be between {minimum} and {maximum}."
    return parsed, None


def _parse_date(value: object, field: str) -> tuple[date | None, str | None]:
    if isinstance(value, datetime):
        return None, f"{field} must be an ISO calendar date."
    if isinstance(value, date):
        return value, None
    try:
        return date.fromisoformat(str(value).strip()), None
    except (TypeError, ValueError):
        return None, f"{field} must be an ISO calendar date."


def _course_readiness_error(session: Session, course_id: int) -> str | None:
    """Return why a course is not ready to anchor a season, or ``None``.

    A qualifying tee is a men's tee with exactly front/back/full ratings and
    a complete valid 18-hole grid, including a yardage at that tee for every
    course hole.  The grid remains course-owned; this check merely proves it
    is ready and never changes it.
    """
    course = session.get(Course, course_id)
    if course is None:
        return "Course does not exist."

    tees = list(
        session.execute(
            select(TeeSet).where(TeeSet.course_id == course_id, TeeSet.gender == "men")
        ).scalars()
    )
    if not tees:
        return "Course must have at least one men's tee set."

    holes = list(
        session.execute(select(Hole).where(Hole.course_id == course_id)).scalars()
    )
    for tee in tees:
        scopes = set(
            session.execute(
                select(TeeRating.scope).where(TeeRating.tee_set_id == tee.id)
            ).scalars()
        )
        if scopes != {"front", "back", "full"}:
            continue
        yards_by_hole = dict(
            session.execute(
                select(HoleYardage.hole_id, HoleYardage.yards).where(
                    HoleYardage.tee_set_id == tee.id
                )
            ).all()
        )
        grid = [
            {
                "number": hole.number,
                "nine": hole.nine,
                "par": hole.par,
                "stroke_index_18": hole.stroke_index_18,
                "stroke_index_9": hole.stroke_index_9,
                "yardages": (
                    {tee.id: yards_by_hole[hole.id]}
                    if hole.id in yards_by_hole
                    else {}
                ),
            }
            for hole in holes
        ]
        if not validate_hole_grid(grid, {tee.id}):
            return None
    return "Course needs a complete men's tee, ratings, holes, and yardages."


def _validate_season_fields(  # noqa: C901
    session: Session,
    *,
    year: object,
    course_id: object,
    start_date: object,
    end_date: object,
    play_weekday: object,
    status: object,
    first_week_nine: object,
) -> tuple[dict[str, str], dict[str, object]]:
    """Validate every field before any season row is mutated."""
    errors: dict[str, str] = {}
    parsed: dict[str, object] = {}

    for key, value, label, minimum, maximum in (
        ("year", year, "Year", 1, 9999),
        ("course_id", course_id, "Course", 1, 2**63 - 1),
        ("play_weekday", play_weekday, "Play weekday", 0, 6),
    ):
        result, error = _parse_whole_number(value, label, minimum, maximum)
        if error:
            errors[key] = error
        else:
            parsed[key] = result

    parsed_start, start_error = _parse_date(start_date, "Start date")
    parsed_end, end_error = _parse_date(end_date, "End date")
    if start_error:
        errors["start_date"] = start_error
    else:
        parsed["start_date"] = parsed_start
    if end_error:
        errors["end_date"] = end_error
    else:
        parsed["end_date"] = parsed_end

    normalized_status = str(status).strip()
    if normalized_status not in VALID_STATUSES:
        errors["status"] = "Status must be draft, active, or complete."
    else:
        parsed["status"] = normalized_status

    normalized_nine = str(first_week_nine).strip()
    if normalized_nine not in VALID_FIRST_WEEK_NINES:
        errors["first_week_nine"] = "First-week nine must be front or back."
    else:
        parsed["first_week_nine"] = normalized_nine

    if parsed_start is not None and parsed_end is not None:
        if parsed_start > parsed_end:
            errors["end_date"] = "End date must be on or after start date."
        else:
            parsed_weekday = parsed.get("play_weekday")
            if parsed_weekday is not None:
                if parsed_start.weekday() != parsed_weekday:
                    errors["start_date"] = "Start date must fall on the selected weekday."
                if parsed_end.weekday() != parsed_weekday:
                    errors["end_date"] = "End date must fall on the selected weekday."
                if (parsed_end - parsed_start).days % 7:
                    errors["end_date"] = "Season dates must span whole weekly intervals."

    parsed_course_id = parsed.get("course_id")
    if parsed_course_id is not None:
        readiness_error = _course_readiness_error(session, int(parsed_course_id))
        if readiness_error:
            errors["course_id"] = readiness_error
    return errors, parsed


def season_display_name(season: Season, settings: Settings) -> str:
    """Return an override or the configured name for display only."""
    return season.name_override or settings.league_name_template.format(season=season.year)


def season_week_count(season: Season) -> int:
    """Return the inclusive number of weekly dates; it is never stored."""
    return ((season.end_date - season.start_date).days // 7) + 1


def create_season(
    session: Session,
    *,
    year: object,
    name_override: object = None,
    course_id: object,
    start_date: object,
    end_date: object,
    play_weekday: object = 3,
    status: object = "draft",
    first_week_nine: object,
) -> Season:
    """Create one validated season atomically, or write nothing."""
    errors, parsed = _validate_season_fields(
        session,
        year=year,
        course_id=course_id,
        start_date=start_date,
        end_date=end_date,
        play_weekday=play_weekday,
        status=status,
        first_week_nine=first_week_nine,
    )
    if errors:
        raise SeasonValidationError(errors)
    override = str(name_override).strip() if name_override is not None else ""
    season = Season(name_override=override or None, **parsed)
    session.add(season)
    session.commit()
    session.refresh(season)
    return season


def get_season(session: Session, season_id: int) -> Season | None:
    return session.get(Season, season_id)


def list_seasons(session: Session) -> list[Season]:
    """Return every season, with the most recent seasons first."""
    return list(
        session.execute(
            select(Season).order_by(
                Season.year.desc(), Season.start_date.desc(), Season.id.desc()
            )
        ).scalars()
    )


def update_season(
    session: Session,
    season_id: int,
    *,
    year: object,
    name_override: object = None,
    course_id: object,
    start_date: object,
    end_date: object,
    play_weekday: object = 3,
    status: object = "draft",
    first_week_nine: object,
) -> Season | None:
    """Update only GL-30 structural fields; future scheduling is excluded."""
    season = get_season(session, season_id)
    if season is None:
        return None
    errors, parsed = _validate_season_fields(
        session,
        year=year,
        course_id=course_id,
        start_date=start_date,
        end_date=end_date,
        play_weekday=play_weekday,
        status=status,
        first_week_nine=first_week_nine,
    )
    parsed_course_id = parsed.get("course_id")
    if (
        parsed_course_id is not None
        and int(parsed_course_id) != season.course_id
        and session.execute(
            select(SeasonParticipant.id)
            .where(SeasonParticipant.season_id == season_id)
            .limit(1)
        ).first()
        is not None
    ):
        errors["course_id"] = (
            "Cannot change the season course while participant overrides exist."
        )
    if session.execute(
        select(Week.id).where(Week.season_id == season_id).limit(1)
    ).first() is not None:
        for field, column in (
            ("start_date", "start_date"),
            ("end_date", "end_date"),
            ("play_weekday", "play_weekday"),
            ("first_week_nine", "first_week_nine"),
        ):
            if column in parsed and parsed[column] != getattr(season, column):
                errors[field] = "Cannot change schedule-defining season fields after weeks exist."
    if errors:
        raise SeasonValidationError(errors)
    season.year = int(parsed["year"])
    season.name_override = str(name_override).strip() or None
    season.course_id = int(parsed["course_id"])
    season.start_date = parsed["start_date"]  # type: ignore[assignment]
    season.end_date = parsed["end_date"]  # type: ignore[assignment]
    season.play_weekday = int(parsed["play_weekday"])
    season.status = str(parsed["status"])
    season.first_week_nine = str(parsed["first_week_nine"])
    session.commit()
    session.refresh(season)
    return season


def list_courses_for_season_form(session: Session) -> list[Course]:
    return list(session.execute(select(Course).order_by(Course.name)).scalars())


__all__ = [
    "SeasonValidationError",
    "create_season",
    "get_season",
    "list_courses_for_season_form",
    "list_seasons",
    "season_display_name",
    "season_week_count",
    "update_season",
]
