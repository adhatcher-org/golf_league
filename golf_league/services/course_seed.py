"""Idempotent, missing-only seed for the league's own course: Wyandot Golf Club.

Never updates an existing row and never deletes one. Missing rows are
filled in; present rows (including anything an admin has edited) are left
exactly as they are. Safe to call on every boot.
"""

import logging
from decimal import Decimal

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.models import Course, Hole, HoleYardage, TeeRating, TeeSet

logger = logging.getLogger(__name__)

COURSE_NAME = "Wyandot Golf Club"
COURSE_WEBSITE = "https://wyandotgc.com/scorecard/"
COURSE_TOTAL_HOLES = 18

TEE_SETS = (
    {"name": "Deer", "color_label": "White", "gender": "men", "total_yards": 5707, "sort_order": 1},
    {"name": "Snake", "color_label": "Gold", "gender": "men", "total_yards": 4967, "sort_order": 2},
)

# Ratings keyed by tee set name, verbatim from the published card.
RATINGS: dict[str, tuple[dict[str, object], ...]] = {
    "Deer": (
        {"scope": "full", "rating": "67.9", "slope": 113, "par": 72},
        {"scope": "front", "rating": "33.9", "slope": 114, "par": 36},
        {"scope": "back", "rating": "34.0", "slope": 111, "par": 36},
    ),
    "Snake": (
        {"scope": "full", "rating": "63.8", "slope": 109, "par": 72},
        {"scope": "front", "rating": "31.9", "slope": 107, "par": 36},
        {"scope": "back", "rating": "31.9", "slope": 110, "par": 36},
    ),
}

# number, stored nine, par, printed 18-hole index, published nine-hole index,
# Deer/White yards, Snake/Gold yards.  The source is Plan/Wyandot Course Data.
HOLES = (
    (1, "front", 5, 9, 5, 498, 425), (2, "front", 4, 6, 3, 332, 307),
    (3, "front", 4, 5, 2, 338, 308), (4, "front", 4, 12, 7, 310, 283),
    (5, "front", 3, 15, 8, 135, 95), (6, "front", 5, 18, 9, 425, 380),
    (7, "front", 4, 7, 4, 353, 310), (8, "front", 3, 11, 6, 128, 110),
    (9, "front", 4, 4, 1, 385, 315), (10, "back", 5, 16, 8, 423, 342),
    (11, "back", 4, 14, 7, 262, 256), (12, "back", 4, 3, 3, 385, 322),
    (13, "back", 3, 13, 6, 139, 129), (14, "back", 4, 10, 5, 284, 262),
    (15, "back", 3, 8, 4, 160, 120), (16, "back", 4, 1, 1, 354, 310),
    (17, "back", 5, 17, 9, 419, 375), (18, "back", 4, 2, 2, 377, 318),
)


def seed_wyandot(session: Session) -> None:  # noqa: C901
    """Create Wyandot Golf Club, its two men's tee sets and six ratings.

    Contract, in order:
    1. Create the course only when no `courses` row with this name exists.
    2. Create each tee set only when no row with this
       `(course_id, name, gender)` exists.
    3. Create each rating only when no row with this
       `(tee_set_id, scope)` exists.
    4. Commit once.
    """
    # This function runs during application boot.  Before the GL-21 migration
    # it must remain a no-op rather than making a query against absent tables.
    if not {"holes", "hole_yardages"} <= set(inspect(session.bind).get_table_names()):
        return
    try:
        course = session.execute(
            select(Course).where(Course.name == COURSE_NAME)
        ).scalar_one_or_none()
        if course is None:
            course = Course(
                name=COURSE_NAME,
                website=COURSE_WEBSITE,
                total_holes=COURSE_TOTAL_HOLES,
            )
            session.add(course)
            session.flush()

        for tee_spec in TEE_SETS:
            tee_set = session.execute(
                select(TeeSet).where(
                    TeeSet.course_id == course.id,
                    TeeSet.name == tee_spec["name"],
                    TeeSet.gender == tee_spec["gender"],
                )
            ).scalar_one_or_none()
            if tee_set is None:
                tee_set = TeeSet(
                    course_id=course.id,
                    name=tee_spec["name"],
                    color_label=tee_spec["color_label"],
                    gender=tee_spec["gender"],
                    total_yards=tee_spec["total_yards"],
                    sort_order=tee_spec["sort_order"],
                )
                session.add(tee_set)
                session.flush()

            for rating_spec in RATINGS[tee_spec["name"]]:
                existing = session.execute(
                    select(TeeRating).where(
                        TeeRating.tee_set_id == tee_set.id,
                        TeeRating.scope == rating_spec["scope"],
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        TeeRating(
                            tee_set_id=tee_set.id,
                            scope=rating_spec["scope"],
                            rating=Decimal(str(rating_spec["rating"])),
                            slope=rating_spec["slope"],
                            par=rating_spec["par"],
                        )
                    )

        tee_by_name = {tee.name: tee for tee in session.execute(select(TeeSet).where(TeeSet.course_id == course.id)).scalars()}
        existing_holes = {hole.number: hole for hole in session.execute(select(Hole).where(Hole.course_id == course.id)).scalars()}
        for number, nine, par, si18, si9, deer_yards, snake_yards in HOLES:
            hole = existing_holes.get(number)
            if hole is None:
                hole = Hole(course_id=course.id, number=number, nine=nine, par=par, stroke_index_18=si18, stroke_index_9=si9)
                session.add(hole)
                session.flush()
            for tee_name, yards in (("Deer", deer_yards), ("Snake", snake_yards)):
                tee = tee_by_name.get(tee_name)
                if tee is not None and session.execute(select(HoleYardage).where(HoleYardage.hole_id == hole.id, HoleYardage.tee_set_id == tee.id)).scalar_one_or_none() is None:
                    session.add(HoleYardage(hole_id=hole.id, tee_set_id=tee.id, yards=yards))

        session.commit()
    except IntegrityError:
        session.rollback()
        logger.info("course seed skipped: a concurrent boot already created these rows")


__all__ = ["seed_wyandot"]
