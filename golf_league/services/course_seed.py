"""Idempotent, missing-only seed for the league's own course: Wyandot Golf Club.

Never updates an existing row and never deletes one. Missing rows are
filled in; present rows (including anything an admin has edited) are left
exactly as they are. Safe to call on every boot.
"""

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.models import Course, TeeRating, TeeSet

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


def seed_wyandot(session: Session) -> None:
    """Create Wyandot Golf Club, its two men's tee sets and six ratings.

    Contract, in order:
    1. Create the course only when no `courses` row with this name exists.
    2. Create each tee set only when no row with this
       `(course_id, name, gender)` exists.
    3. Create each rating only when no row with this
       `(tee_set_id, scope)` exists.
    4. Commit once.
    """
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

        session.commit()
    except IntegrityError:
        session.rollback()
        logger.info("course seed skipped: a concurrent boot already created these rows")


__all__ = ["seed_wyandot"]
