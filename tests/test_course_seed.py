"""Tests for the idempotent Wyandot seed."""

from decimal import Decimal

from golf_league.models import Course, TeeRating, TeeSet
from golf_league.services.course_seed import seed_wyandot


def _wyandot(session):
    return session.query(Course).filter_by(name="Wyandot Golf Club").one()


def test_seed_creates_two_men_tee_sets_and_six_rating_rows(session):
    seed_wyandot(session)

    course = _wyandot(session)
    tee_sets = session.query(TeeSet).filter_by(course_id=course.id).all()
    assert len(tee_sets) == 2
    assert {t.name for t in tee_sets} == {"Deer", "Snake"}
    assert all(t.gender == "men" for t in tee_sets)

    ratings = (
        session.query(TeeRating)
        .join(TeeSet, TeeRating.tee_set_id == TeeSet.id)
        .filter(TeeSet.course_id == course.id)
        .all()
    )
    assert len(ratings) == 6


def test_seeded_rating_values_match_the_published_card(session):
    seed_wyandot(session)
    course = _wyandot(session)

    expected = {
        ("Deer", "full"): (Decimal("67.9"), 113, 72),
        ("Deer", "front"): (Decimal("33.9"), 114, 36),
        ("Deer", "back"): (Decimal("34.0"), 111, 36),
        ("Snake", "full"): (Decimal("63.8"), 109, 72),
        ("Snake", "front"): (Decimal("31.9"), 107, 36),
        ("Snake", "back"): (Decimal("31.9"), 110, 36),
    }

    for tee_set in session.query(TeeSet).filter_by(course_id=course.id).all():
        for rating in session.query(TeeRating).filter_by(tee_set_id=tee_set.id).all():
            key = (tee_set.name, rating.scope)
            exp_rating, exp_slope, exp_par = expected[key]
            assert rating.rating == exp_rating
            assert rating.slope == exp_slope
            assert rating.par == exp_par


def test_seeded_tee_totals_are_5707_and_4967(session):
    seed_wyandot(session)
    course = _wyandot(session)

    totals = {t.name: t.total_yards for t in session.query(TeeSet).filter_by(course_id=course.id)}
    assert totals["Deer"] == 5707
    assert totals["Snake"] == 4967


def test_running_the_seed_twice_creates_no_duplicates_and_keeps_the_same_ids(session):
    seed_wyandot(session)
    course = _wyandot(session)
    first_tee_ids = sorted(t.id for t in session.query(TeeSet).filter_by(course_id=course.id))
    first_rating_ids = sorted(r.id for r in session.query(TeeRating).all())

    seed_wyandot(session)

    assert session.query(Course).filter_by(name="Wyandot Golf Club").count() == 1
    second_tee_ids = sorted(t.id for t in session.query(TeeSet).filter_by(course_id=course.id))
    second_rating_ids = sorted(r.id for r in session.query(TeeRating).all())
    assert first_tee_ids == second_tee_ids
    assert first_rating_ids == second_rating_ids
    assert session.query(TeeSet).filter_by(course_id=course.id).count() == 2
    assert session.query(TeeRating).count() == 6


def test_seed_never_overwrites_an_admin_edited_rating(session):
    seed_wyandot(session)
    course = _wyandot(session)
    deer = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
    full_rating = session.query(TeeRating).filter_by(tee_set_id=deer.id, scope="full").one()

    full_rating.rating = Decimal("99.9")
    session.commit()

    seed_wyandot(session)

    reread = session.query(TeeRating).filter_by(tee_set_id=deer.id, scope="full").one()
    assert reread.rating == Decimal("99.9")


def test_seed_restores_only_the_missing_tee_set(session):
    seed_wyandot(session)
    course = _wyandot(session)
    snake = session.query(TeeSet).filter_by(course_id=course.id, name="Snake").one()
    deer = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
    deer_id = deer.id

    session.query(TeeRating).filter_by(tee_set_id=snake.id).delete()
    session.query(TeeSet).filter_by(id=snake.id).delete()
    session.commit()

    seed_wyandot(session)

    tee_sets = session.query(TeeSet).filter_by(course_id=course.id).all()
    assert {t.name for t in tee_sets} == {"Deer", "Snake"}
    # Deer was untouched: same row id as before.
    untouched_deer = session.query(TeeSet).filter_by(course_id=course.id, name="Deer").one()
    assert untouched_deer.id == deer_id
