"""Service and HTTP acceptance tests for GL-30 season configuration."""

from datetime import date

import pytest
from conftest import _extract_csrf
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.config import Settings
from golf_league.models import Course, HoleYardage, Season, TeeRating
from golf_league.services.seasons import (
    SeasonValidationError,
    create_season,
    season_display_name,
    season_week_count,
    update_season,
)


def _payload(course_id, **overrides):
    payload = {
        "year": "2026",
        "name_override": "",
        "course_id": str(course_id),
        "start_date": "2026-08-27",
        "end_date": "2026-10-29",
        "play_weekday": "3",
        "status": "draft",
        "first_week_nine": "front",
    }
    payload.update(overrides)
    return payload


def test_create_fall_season_round_trips_with_derived_count(session, wyandot_course):
    course = wyandot_course()
    season = create_season(session, **_payload(course.id))

    assert season.year == 2026
    assert season.name_override is None
    assert season.start_date == date(2026, 8, 27)
    assert season.end_date == date(2026, 10, 29)
    assert season.play_weekday == 3
    assert season.status == "draft"
    assert season.first_week_nine == "front"
    assert season_week_count(season) == 10
    assert season_display_name(season, Settings(session_secret="test")) == (
        "St. Paul 2026 Fall Golf League"
    )


def test_override_whitespace_becomes_null_and_custom_template_is_used(session, wyandot_course):
    course = wyandot_course()
    season = create_season(session, **_payload(course.id, name_override="   "))
    assert season.name_override is None
    settings = Settings(session_secret="test", league_name_template="League {season}")
    assert season_display_name(season, settings) == "League 2026"


def test_override_wins_over_configured_template(session, wyandot_course):
    course = wyandot_course()
    season = create_season(session, **_payload(course.id, name_override="Fall League"))
    assert season_display_name(
        season, Settings(session_secret="test", league_name_template="League {season}")
    ) == "Fall League"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("year", ""), ("year", " "), ("year", "²"), ("year", "+2026"),
        ("year", "3.0"), ("year", "0"), ("year", "10000"),
        ("course_id", ""), ("course_id", "²"), ("course_id", "3.0"),
        ("play_weekday", ""), ("play_weekday", "²"), ("play_weekday", "3.0"),
        ("play_weekday", "-1"), ("play_weekday", "7"),
    ],
)
def test_numeric_inputs_are_validated_before_any_write(session, wyandot_course, field, value):
    course = wyandot_course()
    values = _payload(course.id)
    values[field] = value
    with pytest.raises(SeasonValidationError) as exc_info:
        create_season(session, **values)
    assert field in exc_info.value.errors
    assert session.execute(select(Season)).all() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": "not-a-date"},
        {"end_date": "not-a-date"},
        {"end_date": "2026-08-20"},
        {"start_date": "2026-08-28"},
        {"end_date": "2026-10-30"},
        {"end_date": "2026-10-23"},
        {"status": "unknown"},
        {"first_week_nine": "middle"},
    ],
)
def test_date_and_enum_errors_write_nothing(session, wyandot_course, changes):
    course = wyandot_course()
    with pytest.raises(SeasonValidationError):
        create_season(session, **_payload(course.id, **changes))
    assert session.execute(select(Season)).all() == []


def test_two_active_seasons_are_permitted(session, wyandot_course):
    course = wyandot_course()
    first = create_season(session, **_payload(course.id, status="active"))
    second = create_season(
        session,
        **_payload(
            course.id,
            year="2027",
            start_date="2027-08-26",
            end_date="2027-10-28",
            status="active",
        ),
    )
    assert {first.status, second.status} == {"active"}


def test_course_readiness_requires_mens_tee_ratings_holes_and_yardages(
    session, wyandot_course
):
    course = wyandot_course()
    course_id = course.id
    tees = [tee for tee in course.tee_sets if tee.gender == "men"]
    tee_ids = [tee.id for tee in tees]
    session.query(TeeRating).filter(TeeRating.tee_set_id.in_(tee_ids), TeeRating.scope == "full").delete(
        synchronize_session=False
    )
    session.commit()
    with pytest.raises(SeasonValidationError) as exc_info:
        create_season(session, **_payload(course_id))
    assert "course_id" in exc_info.value.errors

    # Restore the rating but remove one required yardage; the course remains unready.
    for tee_id in tee_ids:
        session.add(TeeRating(tee_set_id=tee_id, scope="full", rating="67.9", slope=113, par=72))
    session.query(HoleYardage).filter(HoleYardage.tee_set_id.in_(tee_ids)).delete(
        synchronize_session=False
    )
    session.commit()
    with pytest.raises(SeasonValidationError) as exc_info:
        create_season(session, **_payload(course_id))
    assert "course_id" in exc_info.value.errors


def test_update_is_atomic_and_only_changes_structural_fields(session, wyandot_course):
    course = wyandot_course()
    season = create_season(session, **_payload(course.id))
    with pytest.raises(SeasonValidationError):
        update_season(session, season.id, **_payload(course.id, year="0"))
    session.refresh(season)
    assert season.year == 2026

    updated = update_season(
        session,
        season.id,
        **_payload(course.id, name_override="Fall 2026", first_week_nine="back"),
    )
    assert updated is not None
    assert updated.name_override == "Fall 2026"
    assert updated.first_week_nine == "back"


def test_admin_create_edit_and_authentication_boundaries(admin_client, empty_client):
    anonymous = empty_client.get("/admin/seasons/new")
    assert anonymous.status_code == 401

    page = admin_client.get("/admin/seasons/new")
    assert page.status_code == 200
    csrf_token = _extract_csrf(page.text)
    session = Session(bind=admin_client.app.state.engine)
    try:
        course_id = session.execute(select(Course.id)).scalar_one()
    finally:
        session.close()
    response = admin_client.post(
        "/admin/seasons/new",
        data={**_payload(course_id), "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/seasons/")

    edit_page = admin_client.get(response.headers["location"])
    assert "St. Paul 2026 Fall Golf League" in edit_page.text
    assert "10 weekly dates" in edit_page.text
    edit_csrf = _extract_csrf(edit_page.text)
    edit_response = admin_client.post(
        response.headers["location"],
        data={**_payload(course_id, name_override="Fall 2026", first_week_nine="back"), "csrf_token": edit_csrf},
        follow_redirects=False,
    )
    assert edit_response.status_code == 303


def test_admin_errors_csrf_and_missing_season(admin_client):
    page = admin_client.get("/admin/seasons/new")
    csrf_token = _extract_csrf(page.text)
    invalid = admin_client.post(
        "/admin/seasons/new",
        data={**_payload("bogus"), "csrf_token": csrf_token},
    )
    assert invalid.status_code == 422
    assert "course_id" in invalid.text
    assert admin_client.post("/admin/seasons/new", data=_payload("1")).status_code == 403
    assert admin_client.get("/admin/seasons/999999/edit").status_code == 404
    assert admin_client.post(
        "/admin/seasons/999999/edit", data={"csrf_token": csrf_token}
    ).status_code == 404


def test_config_template_must_include_a_season_field():
    with pytest.raises(ValueError):
        Settings(session_secret="test", league_name_template="Plain league")
    with pytest.raises(ValueError):
        Settings(session_secret="test", league_name_template="{missing}")
    with pytest.raises(ValueError):
        Settings(session_secret="test", league_name_template="{season.missing}")
