"""Session-taking CRUD operations for courses, tee sets and tee ratings.

Routes call these; they never touch `golf_league.models` or run SQL
themselves. Pure validation (is this rating finite, is this gender valid)
lives in `golf_league.domain.course` and is applied here before anything
is written.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.course import (
    VALID_SCOPES,
    to_rating_decimal,
    validate_gender,
    validate_positive_int,
    validate_rating,
)
from golf_league.models import Course, TeeRating, TeeSet


class CourseValidationError(Exception):
    """Raised when input fails validation; carries field -> message errors."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


def _validate_course_fields(name: str, total_holes: object) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not name or not str(name).strip():
        errors["name"] = "Name is required."
    holes_error = validate_positive_int(total_holes, "Total holes")
    if holes_error:
        errors["total_holes"] = holes_error
    return errors


def create_course(
    session: Session,
    *,
    name: str,
    city: str | None = None,
    state: str | None = None,
    website: str | None = None,
    total_holes: int = 18,
) -> Course:
    """Create a course, or raise `CourseValidationError` (writes nothing)."""
    errors = _validate_course_fields(name, total_holes)
    if errors:
        raise CourseValidationError(errors)

    course = Course(
        name=str(name).strip(),
        city=city or None,
        state=state or None,
        website=website or None,
        total_holes=int(total_holes),
    )
    session.add(course)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise CourseValidationError({"name": "A course with this name already exists."}) from None
    session.refresh(course)
    return course


def update_course(
    session: Session,
    course_id: int,
    *,
    name: str,
    city: str | None = None,
    state: str | None = None,
    website: str | None = None,
    total_holes: int = 18,
) -> Course | None:
    """Update a course; returns None when `course_id` does not exist."""
    course = session.get(Course, course_id)
    if course is None:
        return None

    errors = _validate_course_fields(name, total_holes)
    if errors:
        raise CourseValidationError(errors)

    course.name = str(name).strip()
    course.city = city or None
    course.state = state or None
    course.website = website or None
    course.total_holes = int(total_holes)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise CourseValidationError({"name": "A course with this name already exists."}) from None
    session.refresh(course)
    return course


def get_course(session: Session, course_id: int) -> Course | None:
    """Return the `Course` with `course_id`, or None."""
    return session.get(Course, course_id)


def list_courses_with_tee_sets(session: Session) -> list[Course]:
    """Return every course, each with its `tee_sets` populated."""
    return list(session.execute(select(Course).order_by(Course.name)).scalars().all())


def get_tee_set_for_course(
    session: Session, course_id: int, tee_set_id: int
) -> TeeSet | None:
    """Return the `TeeSet` with `tee_set_id`, only when it belongs to `course_id`.

    A tee set belonging to another course is treated identically to a
    missing tee set (returns None), never silently returned.
    """
    tee_set = session.get(TeeSet, tee_set_id)
    if tee_set is None or tee_set.course_id != course_id:
        return None
    return tee_set


def _validate_tee_fields(
    gender: str,
    total_yards: object,
    sort_order: object,
    ratings: dict[str, dict[str, object]],
) -> dict[str, str]:
    errors: dict[str, str] = {}

    gender_error = validate_gender(gender)
    if gender_error:
        errors["gender"] = gender_error

    yards_error = validate_positive_int(total_yards, "Total yards")
    if yards_error:
        errors["total_yards"] = yards_error

    order_error = validate_positive_int(sort_order, "Sort order")
    if order_error:
        errors["sort_order"] = order_error

    missing = set(VALID_SCOPES) - set(ratings.keys())
    if missing:
        errors["scopes"] = f"Missing scope(s): {', '.join(sorted(missing))}."
        return errors

    for scope in VALID_SCOPES:
        entry = ratings[scope]
        rating_error = validate_rating(entry.get("rating"))
        if rating_error:
            errors[f"{scope}_rating"] = rating_error
        slope_error = validate_positive_int(entry.get("slope"), f"{scope} slope")
        if slope_error:
            errors[f"{scope}_slope"] = slope_error
        par_error = validate_positive_int(entry.get("par"), f"{scope} par")
        if par_error:
            errors[f"{scope}_par"] = par_error

    return errors


def create_tee_set_with_ratings(
    session: Session,
    course_id: int,
    *,
    name: str,
    color_label: str,
    gender: str,
    total_yards: object,
    sort_order: object,
    ratings: dict[str, dict[str, object]],
) -> TeeSet:
    """Create one tee set and its three ratings atomically.

    Raises `CourseValidationError` and writes nothing when validation fails
    or the course does not exist (callers check the course exists first if
    they need a 404 rather than a 422).
    """
    errors = _validate_tee_fields(gender, total_yards, sort_order, ratings)
    if not name or not str(name).strip():
        errors["name"] = "Name is required."
    if not color_label or not str(color_label).strip():
        errors["color_label"] = "Color label is required."
    if errors:
        raise CourseValidationError(errors)

    tee_set = TeeSet(
        course_id=course_id,
        name=str(name).strip(),
        color_label=str(color_label).strip(),
        gender=gender,
        total_yards=int(total_yards),
        sort_order=int(sort_order),
    )
    session.add(tee_set)
    try:
        session.flush()
        for scope in VALID_SCOPES:
            entry = ratings[scope]
            session.add(
                TeeRating(
                    tee_set_id=tee_set.id,
                    scope=scope,
                    rating=to_rating_decimal(entry["rating"]),
                    slope=int(entry["slope"]),
                    par=int(entry["par"]),
                )
            )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise CourseValidationError(
            {"name": "A tee set with this name and gender already exists for this course."}
        ) from None
    session.refresh(tee_set)
    return tee_set


def update_tee_set_with_ratings(
    session: Session,
    course_id: int,
    tee_set_id: int,
    *,
    name: str,
    color_label: str,
    gender: str,
    total_yards: object,
    sort_order: object,
    ratings: dict[str, dict[str, object]],
) -> TeeSet | None:
    """Update a tee set and all three ratings atomically.

    Returns None when the tee set does not exist, or exists but belongs to
    a different course (a foreign tee is never silently edited). Raises
    `CourseValidationError` and writes nothing on validation failure.
    """
    tee_set = get_tee_set_for_course(session, course_id, tee_set_id)
    if tee_set is None:
        return None

    errors = _validate_tee_fields(gender, total_yards, sort_order, ratings)
    if not name or not str(name).strip():
        errors["name"] = "Name is required."
    if not color_label or not str(color_label).strip():
        errors["color_label"] = "Color label is required."
    if errors:
        raise CourseValidationError(errors)

    existing_ratings = {
        rating.scope: rating
        for rating in session.execute(
            select(TeeRating).where(TeeRating.tee_set_id == tee_set_id)
        ).scalars()
    }

    tee_set.name = str(name).strip()
    tee_set.color_label = str(color_label).strip()
    tee_set.gender = gender
    tee_set.total_yards = int(total_yards)
    tee_set.sort_order = int(sort_order)

    try:
        for scope in VALID_SCOPES:
            entry = ratings[scope]
            rating_value: Decimal = to_rating_decimal(entry["rating"])
            if scope in existing_ratings:
                row = existing_ratings[scope]
                row.rating = rating_value
                row.slope = int(entry["slope"])
                row.par = int(entry["par"])
            else:
                session.add(
                    TeeRating(
                        tee_set_id=tee_set_id,
                        scope=scope,
                        rating=rating_value,
                        slope=int(entry["slope"]),
                        par=int(entry["par"]),
                    )
                )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise CourseValidationError(
            {"name": "A tee set with this name and gender already exists for this course."}
        ) from None
    session.refresh(tee_set)
    return tee_set


__all__ = [
    "CourseValidationError",
    "create_course",
    "update_course",
    "get_course",
    "list_courses_with_tee_sets",
    "get_tee_set_for_course",
    "create_tee_set_with_ratings",
    "update_tee_set_with_ratings",
]
