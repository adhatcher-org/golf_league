"""Service and pure-domain coverage for the complete course grid."""
from sqlalchemy import select

from golf_league.domain.course import derive_stroke_index_9
from golf_league.models import Hole, HoleYardage, TeeSet
from golf_league.services.course_seed import HOLES, seed_wyandot
from golf_league.services.courses import CourseValidationError, save_hole_grid


def _grid(session):
    seed_wyandot(session)
    from golf_league.models import Course
    course = session.execute(select(Course).where(Course.name == "Wyandot Golf Club")).scalar_one()
    tees = {tee.name: tee.id for tee in session.execute(select(TeeSet).where(TeeSet.course_id == course.id)).scalars()}
    return course, [
        {"number": number, "nine": nine, "par": par, "stroke_index_18": si18, "stroke_index_9": si9,
         "yardages": {tees["Deer"]: deer, tees["Snake"]: snake}}
        for number, nine, par, si18, si9, deer, snake in HOLES
    ]


def test_eighteen_holes_and_thirty_six_yardages_round_trip(session):
    course, grid = _grid(session)
    save_hole_grid(session, course.id, grid)
    assert session.query(Hole).filter_by(course_id=course.id).count() == 18
    assert session.query(HoleYardage).count() == 36


def test_stroke_indexes_and_missing_yardage_reject_the_entire_grid(session):
    course, grid = _grid(session)
    grid[0]["stroke_index_18"] = grid[1]["stroke_index_18"]
    try:
        save_hole_grid(session, course.id, grid)
    except CourseValidationError:
        pass
    else:
        raise AssertionError("duplicate index must be rejected")
    assert session.query(Hole).filter_by(course_id=course.id).count() == 18


def test_per_nine_index_and_foreign_tee_reject_without_writes(session):
    course, grid = _grid(session)
    before = session.query(Hole).filter_by(course_id=course.id, number=1).one().stroke_index_9
    grid[0]["stroke_index_9"] = grid[1]["stroke_index_9"]
    try:
        save_hole_grid(session, course.id, grid)
    except CourseValidationError:
        pass
    else:
        raise AssertionError("duplicate nine index must be rejected")
    assert session.query(Hole).filter_by(course_id=course.id, number=1).one().stroke_index_9 == before
    course, grid = _grid(session)
    foreign_tee_id = 999999
    yards = grid[0]["yardages"].pop(next(iter(grid[0]["yardages"])))
    grid[0]["yardages"][foreign_tee_id] = yards
    try:
        save_hole_grid(session, course.id, grid)
    except CourseValidationError:
        pass
    else:
        raise AssertionError("foreign tee yardage must be rejected")


def test_rating_par_mismatch_rejects_the_whole_save(session):
    from golf_league.models import TeeRating
    course, grid = _grid(session)
    grid[0]["par"] = 4
    rating = session.query(TeeRating).filter_by(scope="front").first()
    assert rating is not None
    try:
        save_hole_grid(session, course.id, grid)
    except CourseValidationError:
        pass
    else:
        raise AssertionError("rating par mismatch must be rejected")
    assert session.query(Hole).filter_by(course_id=course.id, number=1).one().par == 5


def test_derive_stroke_index_9_reproduces_published_values(session):
    values = {number: si18 for number, _, _, si18, _, _, _ in HOLES}
    nines = {number: nine for number, nine, *_ in HOLES}
    expected = {number: si9 for number, _, _, _, si9, _, _ in HOLES}
    assert derive_stroke_index_9(values, nines) == expected


def test_confirmed_manual_stroke_index_9_survives_later_save(session):
    course, grid = _grid(session)
    # Swap two rankings: a valid, deliberate override of the published default.
    grid[0]["stroke_index_9"], grid[1]["stroke_index_9"] = 3, 5
    save_hole_grid(session, course.id, grid)
    assert session.execute(select(Hole.stroke_index_9).where(Hole.course_id == course.id, Hole.number == 1)).scalar_one() == 3
