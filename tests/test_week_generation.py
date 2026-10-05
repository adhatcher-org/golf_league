"""GL-33 pure generation, persistence, guards and admin contracts."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from conftest import _extract_csrf
from sqlalchemy import select

from golf_league.domain.schedule import generate_weeks
from golf_league.models import Season, Week
from golf_league.services.schedule import (
    ScheduleConflict,
    ScheduleValidationError,
    build_default_proposal,
    commit_generated_weeks,
    delete_week,
    edit_week,
    list_weeks,
    schedule_fingerprint,
    week_fingerprint,
)
from golf_league.services.seasons import (
    SeasonValidationError,
    create_season,
    update_season,
)


def _season(session, course):
    return create_season(
        session,
        year=2026,
        name_override="",
        course_id=course.id,
        start_date="2026-08-27",
        end_date="2026-10-29",
        play_weekday=3,
        status="draft",
        first_week_nine="front",
    )


def _commit(session, season, proposal=None):
    return commit_generated_weeks(
        session,
        season.id,
        proposal=proposal or build_default_proposal(season, 10),
        expected_fingerprint=schedule_fingerprint(season),
    )


def test_pure_generator_alternates_by_printed_index_across_calendar_boundaries():
    assert generate_weeks(date(2026, 12, 31), 3, 3, "back") == [
        (date(2026, 12, 31), "back"),
        (date(2027, 1, 7), "front"),
        (date(2027, 1, 14), "back"),
    ]
    with pytest.raises(ValueError):
        generate_weeks(date(2026, 8, 28), 3, 2, "front")
    with pytest.raises(ValueError):
        generate_weeks(date(2026, 8, 27), 3, 0, "front")


def test_fall_proposal_commits_exact_dates_nines_and_exceptions(session, wyandot_course):
    season = _season(session, wyandot_course())
    proposal = build_default_proposal(season, "10")
    for index in (1, 6):
        proposal[index - 1] = replace(proposal[index - 1], week_type="play_with_team")
    proposal[0] = replace(proposal[0], status="cancelled")
    proposal[9] = replace(proposal[9], week_type="rain_date", nine=None, makeup_for_index=9)
    weeks = _commit(session, season, proposal)
    assert len(weeks) == 10
    assert [(w.index, w.play_date, w.nine) for w in weeks] == [
        (i, date(2026, 8, 27) + timedelta(days=7 * (i - 1)), "front" if i % 2 else "back")
        for i in range(1, 10)
    ] + [(10, date(2026, 10, 29), None)]
    assert weeks[0].week_type == "play_with_team" and weeks[0].status == "cancelled"
    assert weeks[5].week_type == "play_with_team"
    assert weeks[9].week_type == "rain_date" and weeks[9].makeup_for_week_id == weeks[8].id


def test_generation_rejects_count_and_nonalternating_schedule_without_writes(session, wyandot_course):
    season = _season(session, wyandot_course())
    with pytest.raises(ScheduleValidationError, match="Week count"):
        build_default_proposal(season, 9)
    proposal = build_default_proposal(season, 10)
    proposal[1] = replace(proposal[1], nine="front")
    with pytest.raises(ScheduleValidationError, match="alternate"):
        _commit(session, season, proposal)
    assert list_weeks(session, season.id) == []


def test_played_status_is_reserved_for_future_workflows(session, wyandot_course):
    season = _season(session, wyandot_course())
    proposal = build_default_proposal(season, 10)
    proposal[0] = replace(proposal[0], status="played")
    with pytest.raises(ScheduleValidationError, match="cannot be marked played"):
        _commit(session, season, proposal)


def test_makeup_rejects_self_and_cycles(session, wyandot_course):
    season = _season(session, wyandot_course())
    proposal = build_default_proposal(season, 10)
    proposal[8] = replace(proposal[8], makeup_for_index=10)
    proposal[9] = replace(proposal[9], week_type="rain_date", nine=None, makeup_for_index=9)
    with pytest.raises(ScheduleValidationError, match="cycle"):
        _commit(session, season, proposal)
    proposal[9] = replace(proposal[9], makeup_for_index=10)
    with pytest.raises(ScheduleValidationError, match="another week"):
        _commit(session, season, proposal)


def test_generation_repeat_stale_preview_and_foreign_makeup_are_refused(session, wyandot_course):
    season = _season(session, wyandot_course())
    rows = _commit(session, season)
    rows[0].notes = "preserve me"
    session.commit()
    with pytest.raises(ScheduleConflict, match="already has weeks"):
        _commit(session, season)
    assert len(list_weeks(session, season.id)) == 10
    assert session.get(Week, rows[0].id).notes == "preserve me"
    season.first_week_nine = "back"
    session.flush()
    with pytest.raises(ScheduleConflict, match="changed after this preview"):
        commit_generated_weeks(session, season.id, proposal=[], expected_fingerprint="stale")


def test_week_edit_is_stale_safe_and_delete_refuses_makeup_references(session, wyandot_course):
    season = _season(session, wyandot_course())
    proposal = build_default_proposal(season, 10)
    proposal[9] = replace(proposal[9], week_type="rain_date", nine=None)
    rows = _commit(session, season, proposal)
    target, rain = rows[8], rows[9]
    edit_week(
        session, season.id, rain.id, week_type="rain_date", status="scheduled",
        makeup_for_index="9", notes="makeup", expected_fingerprint=week_fingerprint(rain),
    )
    with pytest.raises(ScheduleConflict, match="makeup target"):
        delete_week(session, season.id, target.id)
    with pytest.raises(ScheduleConflict, match="changed after"):
        edit_week(
            session, season.id, rain.id, week_type="rain_date", status="scheduled",
            makeup_for_index="", notes="", expected_fingerprint="stale",
        )
    delete_week(session, season.id, rain.id)
    assert len(list_weeks(session, season.id)) == 9
    assert list_weeks(session, season.id)[-1].index == 9


def test_week_edit_rejects_cycle_that_reaches_edited_week_with_no_current_makeup(session, wyandot_course):
    season = _season(session, wyandot_course())
    rows = _commit(session, season)
    week_a, week_b = rows[0], rows[1]
    edit_week(
        session, season.id, week_b.id, week_type="match", status="scheduled",
        makeup_for_index="1", notes="", expected_fingerprint=week_fingerprint(week_b),
    )
    assert week_a.makeup_for_week_id is None
    with pytest.raises(ScheduleValidationError, match="cannot form a cycle"):
        edit_week(
            session, season.id, week_a.id, week_type="match", status="scheduled",
            makeup_for_index="2", notes="", expected_fingerprint=week_fingerprint(week_a),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("start_date", "2026-09-03"), ("end_date", "2026-11-05"),
     ("play_weekday", "4"), ("first_week_nine", "back")],
)
def test_season_schedule_fields_are_guarded_after_weeks_exist(session, wyandot_course, field, value):
    season = _season(session, wyandot_course())
    _commit(session, season)
    data = {
        "year": season.year,
        "name_override": "Renamed",
        "course_id": season.course_id,
        "start_date": season.start_date.isoformat(),
        "end_date": season.end_date.isoformat(),
        "play_weekday": season.play_weekday,
        "status": "active",
        "first_week_nine": season.first_week_nine,
    }
    data[field] = value
    with pytest.raises(SeasonValidationError) as exc:
        update_season(session, season.id, **data)
    assert field in exc.value.errors
    assert session.get(Season, season.id).name_override is None


def test_admin_preview_commit_csrf_and_repeat_contract(admin_client):
    client = admin_client
    season_page = client.get("/admin/seasons")
    assert season_page.status_code == 200
    csrf = _extract_csrf(client.get("/admin/seasons/new").text)
    from golf_league.models import Course
    db = client.app.state.engine
    from sqlalchemy.orm import Session
    with Session(bind=db) as session:
        course_id = session.execute(select(Course.id).where(Course.name == "Wyandot Golf Club")).scalar_one()
    form = {"year": "2026", "name_override": "", "course_id": str(course_id),
            "start_date": "2026-08-27", "end_date": "2026-10-29", "play_weekday": "3",
            "status": "draft", "first_week_nine": "front", "csrf_token": csrf}
    response = client.post("/admin/seasons/new", data=form, follow_redirects=False)
    assert response.status_code == 303
    season_id = int(response.headers["location"].rstrip("/").split("/")[-1]) if "/edit" not in response.headers["location"] else int(response.headers["location"].split("/")[-2])
    listing = client.get(f"/admin/seasons/{season_id}/weeks")
    token = _extract_csrf(listing.text)
    preview = client.post(f"/admin/seasons/{season_id}/weeks/generate", data={"csrf_token": token, "count": "10", "action": "preview"})
    assert preview.status_code == 200 and "Review schedule preview" in preview.text
    with Session(bind=db) as session:
        assert session.execute(select(Week).where(Week.season_id == season_id)).all() == []
    commit_data = {
        "csrf_token": _extract_csrf(preview.text),
        "count": "10",
        "action": "commit",
        "week_type_1": "play_with_team",
        "status_1": "cancelled",
        "week_type_6": "play_with_team",
        "week_type_10": "rain_date",
        "makeup_10": "9",
    }
    import re
    commit_data["season_fingerprint"] = re.search(r'name="season_fingerprint" value="([^"]+)', preview.text).group(1)
    committed = client.post(f"/admin/seasons/{season_id}/weeks/generate", data=commit_data, follow_redirects=False)
    assert committed.status_code == 303
    again = client.get(f"/admin/seasons/{season_id}/weeks")
    assert "Makeup for" in again.text and "Week 9" in again.text
    with Session(bind=db) as session:
        weeks = list_weeks(session, season_id)
        week_two, week_nine, week_ten = weeks[1], weeks[8], weeks[9]
        week_two_id, week_nine_id, week_ten_id = week_two.id, week_nine.id, week_ten.id
    edit_page = client.get(f"/admin/seasons/{season_id}/weeks/{week_two_id}/edit")
    edit_token = _extract_csrf(edit_page.text)
    import re
    fingerprint = re.search(r'name="fingerprint" value="([^"]+)', edit_page.text).group(1)
    edited = client.post(
        f"/admin/seasons/{season_id}/weeks/{week_two_id}/edit",
        data={"csrf_token": edit_token, "fingerprint": fingerprint, "week_type": "match",
              "status": "scheduled", "makeup_for_index": "", "notes": "Synthetic edit"},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    delete_target = client.post(
        f"/admin/seasons/{season_id}/weeks/{week_nine_id}/delete",
        data={"csrf_token": edit_token},
    )
    assert delete_target.status_code == 409
    delete_makeup = client.post(
        f"/admin/seasons/{season_id}/weeks/{week_ten_id}/delete",
        data={"csrf_token": edit_token}, follow_redirects=False,
    )
    assert delete_makeup.status_code == 303
    with Session(bind=db) as session:
        assert session.get(Week, week_two_id).notes == "Synthetic edit"
        assert [row.index for row in list_weeks(session, season_id)][-1] == 9
    repeated = client.post(f"/admin/seasons/{season_id}/weeks/generate", data={"csrf_token": _extract_csrf(again.text), "count": "10", "action": "preview"})
    assert repeated.status_code == 409 and "already has weeks" in repeated.text
    no_csrf = client.post(f"/admin/seasons/{season_id}/weeks/generate", data={"count": "10", "action": "preview"})
    assert no_csrf.status_code == 403


def test_weeks_routes_require_admin(client):
    assert client.get("/admin/seasons/1/weeks").status_code == 401
    assert client.post("/admin/seasons/1/weeks/generate", data={"action": "preview"}).status_code == 401
