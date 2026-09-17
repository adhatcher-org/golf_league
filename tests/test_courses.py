"""Model and service level tests for courses, tee sets and tee ratings."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from golf_league.models import TeeRating, TeeSet
from golf_league.services.courses import (
    CourseValidationError,
    create_course,
    create_tee_set_with_ratings,
    get_tee_set_for_course,
    update_tee_set_with_ratings,
)

FULL_RATINGS = {
    "front": {"rating": "33.9", "slope": 114, "par": 36},
    "back": {"rating": "34.0", "slope": 111, "par": 36},
    "full": {"rating": "67.9", "slope": 113, "par": 72},
}


def _make_course(session, name="Test Course"):
    return create_course(session, name=name, website="https://example.test/scorecard")


def test_course_tee_and_three_ratings_persist_and_read_back(session):
    course = _make_course(session)

    tee_set = create_tee_set_with_ratings(
        session,
        course.id,
        name="Deer",
        color_label="White",
        gender="men",
        total_yards=5707,
        sort_order=1,
        ratings=FULL_RATINGS,
    )

    fetched = session.get(TeeSet, tee_set.id)
    assert fetched is not None
    assert fetched.course_id == course.id
    ratings = session.query(TeeRating).filter_by(tee_set_id=tee_set.id).all()
    assert len(ratings) == 3
    assert {r.scope for r in ratings} == {"front", "back", "full"}


def test_duplicate_tee_name_and_gender_in_one_course_is_rejected(session):
    course = _make_course(session)
    create_tee_set_with_ratings(
        session, course.id, name="Deer", color_label="White", gender="men",
        total_yards=5707, sort_order=1, ratings=FULL_RATINGS,
    )

    with pytest.raises(CourseValidationError):
        create_tee_set_with_ratings(
            session, course.id, name="Deer", color_label="White", gender="men",
            total_yards=5707, sort_order=2, ratings=FULL_RATINGS,
        )


def test_same_tee_name_for_a_different_gender_is_allowed(session):
    course = _make_course(session)
    create_tee_set_with_ratings(
        session, course.id, name="Deer", color_label="White", gender="men",
        total_yards=5707, sort_order=1, ratings=FULL_RATINGS,
    )

    tee_set = create_tee_set_with_ratings(
        session, course.id, name="Deer", color_label="Red", gender="women",
        total_yards=4900, sort_order=2, ratings=FULL_RATINGS,
    )
    assert tee_set.gender == "women"


def test_duplicate_scope_for_one_tee_set_is_rejected(session):
    course = _make_course(session)
    tee_set = create_tee_set_with_ratings(
        session, course.id, name="Deer", color_label="White", gender="men",
        total_yards=5707, sort_order=1, ratings=FULL_RATINGS,
    )

    session.add(TeeRating(tee_set_id=tee_set.id, scope="full", rating=Decimal("70.0"), slope=120, par=72))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_saving_a_tee_with_only_two_scopes_rolls_back_and_writes_nothing(session):
    course = _make_course(session)
    incomplete = {"front": FULL_RATINGS["front"], "back": FULL_RATINGS["back"]}

    with pytest.raises(CourseValidationError):
        create_tee_set_with_ratings(
            session, course.id, name="Deer", color_label="White", gender="men",
            total_yards=5707, sort_order=1, ratings=incomplete,
        )

    assert session.query(TeeSet).count() == 0
    assert session.query(TeeRating).count() == 0


def test_rating_34_0_reads_back_as_34_0_not_34(session):
    course = _make_course(session)
    tee_set = create_tee_set_with_ratings(
        session, course.id, name="Deer", color_label="White", gender="men",
        total_yards=5707, sort_order=1, ratings=FULL_RATINGS,
    )
    back_rating = (
        session.query(TeeRating)
        .filter_by(tee_set_id=tee_set.id, scope="back")
        .one()
    )
    assert back_rating.rating == Decimal("34.0")
    assert str(back_rating.rating) == "34.0"


def test_zero_and_negative_slope_are_rejected(session):
    course = _make_course(session)
    bad_ratings = dict(FULL_RATINGS)
    bad_ratings["front"] = {"rating": "33.9", "slope": 0, "par": 36}

    with pytest.raises(CourseValidationError):
        create_tee_set_with_ratings(
            session, course.id, name="Deer", color_label="White", gender="men",
            total_yards=5707, sort_order=1, ratings=bad_ratings,
        )

    bad_ratings["front"] = {"rating": "33.9", "slope": -5, "par": 36}
    with pytest.raises(CourseValidationError):
        create_tee_set_with_ratings(
            session, course.id, name="Deer", color_label="White", gender="men",
            total_yards=5707, sort_order=1, ratings=bad_ratings,
        )

    assert session.query(TeeSet).count() == 0


@pytest.mark.parametrize(
    "bad_value", ["", "abc", "NaN", "Infinity", "-NaN", "sNaN", "-Infinity"]
)
def test_non_finite_rating_is_rejected(session, bad_value):
    course = _make_course(session)
    bad_ratings = dict(FULL_RATINGS)
    bad_ratings["front"] = {"rating": bad_value, "slope": 114, "par": 36}

    with pytest.raises(CourseValidationError):
        create_tee_set_with_ratings(
            session, course.id, name="Deer", color_label="White", gender="men",
            total_yards=5707, sort_order=1, ratings=bad_ratings,
        )

    assert session.query(TeeSet).count() == 0


def test_tee_set_lookup_is_scoped_to_its_course_and_returns_none_for_a_foreign_id(session):
    course_a = _make_course(session, "Course A")
    course_b = _make_course(session, "Course B")

    tee_set = create_tee_set_with_ratings(
        session, course_a.id, name="Deer", color_label="White", gender="men",
        total_yards=5707, sort_order=1, ratings=FULL_RATINGS,
    )

    assert get_tee_set_for_course(session, course_a.id, tee_set.id) is not None
    assert get_tee_set_for_course(session, course_b.id, tee_set.id) is None
    assert get_tee_set_for_course(session, course_a.id, 999999) is None


def test_update_tee_set_with_ratings_returns_none_for_a_foreign_course_id(session):
    course_a = _make_course(session, "Course A")
    course_b = _make_course(session, "Course B")

    tee_set = create_tee_set_with_ratings(
        session, course_a.id, name="Deer", color_label="White", gender="men",
        total_yards=5707, sort_order=1, ratings=FULL_RATINGS,
    )

    result = update_tee_set_with_ratings(
        session, course_b.id, tee_set.id,
        name="Deer", color_label="White", gender="men",
        total_yards=6000, sort_order=1, ratings=FULL_RATINGS,
    )
    assert result is None

    unchanged = session.get(TeeSet, tee_set.id)
    assert unchanged.total_yards == 5707
    assert unchanged.course_id == course_a.id
