"""GL-31 participant override, role derivation and admin route tests."""

from datetime import date

import pytest
from conftest import _extract_csrf
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.roster import filter_sub_pool, role_from_membership
from golf_league.models import (
    Course,
    Golfer,
    Season,
    SeasonGolfer,
    SeasonParticipant,
    TeeSet,
)
from golf_league.services.participants import (
    ParticipantValidationError,
    add_participant_override,
    effective_participant,
    list_available_golfers,
    update_participant_override,
)
from golf_league.services.roster import delete_golfer
from golf_league.services.season_roster import (
    SeasonRosterValidationError,
    list_season_golfers,
    save_season_golfers,
)


def _season(session, course_id):
    row = Season(
        year=2026, course_id=course_id, start_date=date(2026, 8, 27),
        end_date=date(2026, 10, 29), play_weekday=3, status="draft",
        first_week_nine="front",
    )
    session.add(row)
    session.commit()
    return row


def _golfer(session, tee, *, strokes=6, active=True, first="Sample"):
    row = Golfer(
        first_name=first, last_name="Golfer", default_tee_set_id=tee.id,
        handicap_strokes=strokes, handicap_source="self_reported",
        handicap_status="needs_contact" if strokes is None else "ok",
        is_active=active,
    )
    session.add(row)
    session.commit()
    return row


def test_role_and_sub_pool_are_derived_from_membership_facts(golfer):
    inactive = {"id": 900, "is_active": False}
    member = {"golfer_id": golfer.id}
    assert role_from_membership(True) == "active"
    assert role_from_membership(False) == "sub"
    assert filter_sub_pool([golfer, inactive], [member]) == []
    assert filter_sub_pool([golfer, inactive], []) == [golfer]


def test_optional_override_falls_back_and_seed_override_accepts_signed_and_zero(
    session, wyandot_course, golfer
):
    golfer.handicap_strokes = 6
    session.flush()
    season = _season(session, golfer.default_tee_set.course_id)
    effective = effective_participant(session, season.id, golfer.id)
    assert effective.participant is None
    assert effective.effective_tee.id == golfer.default_tee_set_id
    assert effective.effective_handicap == 6

    zero = add_participant_override(
        session, season.id, golfer_id=golfer.id, seed_handicap_strokes="0",
        seed_source="Fall sheet",
    )
    assert zero.seed_handicap_strokes == 0
    assert effective_participant(session, season.id, golfer.id).effective_handicap == 0

    session.delete(zero)
    session.commit()
    negative = add_participant_override(
        session, season.id, golfer_id=golfer.id, seed_handicap_strokes="-3"
    )
    assert negative.seed_handicap_strokes == -3
    assert effective_participant(session, season.id, golfer.id).effective_handicap == -3


def test_different_tee_without_seed_persists_as_missing_and_clear_never_carries_old_value(
    session, wyandot_course, golfer
):
    course = wyandot_course()
    season = _season(session, course.id)
    snake = next(tee for tee in course.tee_sets if tee.name == "Snake")
    row = add_participant_override(session, season.id, golfer_id=golfer.id, tee_set_id=snake.id)
    effective = effective_participant(session, season.id, golfer.id)
    assert effective.effective_tee.id == snake.id
    assert effective.effective_handicap is None

    updated = update_participant_override(
        session, season.id, row.id, tee_action="keep", tee_set_id="",
        seed_action="set", seed_handicap_strokes="-3", seed_source="new tee entry",
    )
    assert updated.seed_handicap_strokes == -3
    assert effective_participant(session, season.id, golfer.id).effective_handicap == -3
    cleared = update_participant_override(
        session, season.id, row.id, tee_action="keep", tee_set_id="",
        seed_action="clear", seed_handicap_strokes="", seed_source="",
    )
    assert cleared.tee_set_id == snake.id
    assert cleared.seed_handicap_strokes is None
    assert effective_participant(session, season.id, golfer.id).effective_handicap is None


def test_edit_can_keep_or_clear_seed_override_to_roster_fallback(session, wyandot_course, golfer):
    season = _season(session, golfer.default_tee_set.course_id)
    row = add_participant_override(
        session, season.id, golfer_id=golfer.id, seed_handicap_strokes="0",
        seed_source="Fall team sheet",
    )
    kept = update_participant_override(
        session, season.id, row.id, tee_action="keep", tee_set_id="",
        seed_action="keep", seed_handicap_strokes="", seed_source="",
    )
    assert kept.seed_handicap_strokes == 0
    assert kept.seed_source == "Fall team sheet"
    removed = update_participant_override(
        session, season.id, row.id, tee_action="keep", tee_set_id="",
        seed_action="clear", seed_handicap_strokes="", seed_source="",
    )
    assert removed is None
    assert effective_participant(session, season.id, golfer.id).effective_handicap == 12


def test_active_roster_golfer_without_override_remains_available(session, wyandot_course, golfer):
    season = _season(session, golfer.default_tee_set.course_id)
    assert [item.id for item in list_available_golfers(session, season.id)] == [golfer.id]
    golfer.is_active = False
    session.flush()
    assert list_available_golfers(session, season.id) == []


def test_season_roster_bulk_selection_saves_only_selected_active_golfers(
    session, wyandot_course
):
    course = wyandot_course()
    season = _season(session, course.id)
    tee = course.tee_sets[0]
    selected = _golfer(session, tee, first="Selected")
    omitted = _golfer(session, tee, first="Omitted")
    inactive = _golfer(session, tee, first="Inactive", active=False)

    save_season_golfers(session, season.id, [str(selected.id)])

    views = {item.golfer.id: item for item in list_season_golfers(session, season.id)}
    assert views[selected.id].included is True
    assert views[omitted.id].included is False
    assert views[inactive.id].included is False
    assert session.query(SeasonGolfer).filter_by(season_id=season.id).count() == 1


def test_admin_season_roster_post_persists_repeated_checkbox_values(admin_client):
    session = Session(bind=admin_client.app.state.engine)
    try:
        course = session.execute(select(Course)).scalars().first()
        tee = session.execute(
            select(TeeSet).where(TeeSet.course_id == course.id)
        ).scalars().first()
        season = _season(session, course.id)
        first = _golfer(session, tee, first="First")
        second = _golfer(session, tee, first="Second")
        season_id = season.id
        selected_ids = (first.id, second.id)
    finally:
        session.close()

    page = admin_client.get(f"/admin/seasons/{season_id}/participants")
    payload = {
        "csrf_token": _extract_csrf(page.text),
        "golfer_ids": [str(golfer_id) for golfer_id in selected_ids],
    }
    response = admin_client.post(
        f"/admin/seasons/{season_id}/participants/roster",
        data=payload,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/admin/seasons/{season_id}/participants?roster_saved=1"
    )
    saved_page = admin_client.get(response.headers["location"])
    assert saved_page.status_code == 200
    assert "Season roster saved." in saved_page.text

    session = Session(bind=admin_client.app.state.engine)
    try:
        saved_ids = set(session.scalars(
            select(SeasonGolfer.golfer_id).where(SeasonGolfer.season_id == season_id)
        ))
        assert saved_ids == set(selected_ids)
    finally:
        session.close()


def test_season_roster_rejects_inactive_ids_and_preserves_team_members(
    session, wyandot_course
):
    from golf_league.models import Team, TeamMember

    course = wyandot_course()
    season = _season(session, course.id)
    tee = course.tee_sets[0]
    selected = _golfer(session, tee, first="Assigned")
    inactive = _golfer(session, tee, first="Inactive", active=False)
    save_season_golfers(session, season.id, [str(selected.id)])
    team = Team(season_id=season.id, name="Team", number=1, sort_order=1)
    session.add(team)
    session.flush()
    session.add(TeamMember(team_id=team.id, season_id=season.id, golfer_id=selected.id, position=1))
    session.commit()

    with pytest.raises(SeasonRosterValidationError):
        save_season_golfers(session, season.id, [str(inactive.id)])

    save_season_golfers(session, season.id, [])
    assert session.query(SeasonGolfer).filter_by(
        season_id=season.id, golfer_id=selected.id
    ).one()


def test_cross_course_tee_is_rejected_and_tee_delete_is_blocked(session, wyandot_course, golfer):
    course = wyandot_course()
    season = _season(session, course.id)
    other = Course(name="Other Course", total_holes=18)
    session.add(other)
    session.flush()
    foreign_tee = TeeSet(
        course_id=other.id, name="Away", color_label="Away", gender="men",
        total_yards=1000, sort_order=0,
    )
    session.add(foreign_tee)
    session.commit()
    with pytest.raises(ParticipantValidationError, match="this season's course"):
        add_participant_override(session, season.id, golfer_id=golfer.id, tee_set_id=foreign_tee.id)

    local_tee = next(tee for tee in course.tee_sets if tee.name == "Snake")
    row = add_participant_override(
        session, season.id, golfer_id=golfer.id, tee_set_id=local_tee.id,
        seed_handicap_strokes="12",
    )
    from golf_league.services.courses import CourseValidationError, delete_tee_set
    with pytest.raises(CourseValidationError, match="season participant overrides"):
        delete_tee_set(session, course.id, local_tee.id)
    assert session.get(SeasonParticipant, row.id) is not None


def test_golfer_delete_refuses_season_participant_reference(session, wyandot_course, golfer):
    season = _season(session, golfer.default_tee_set.course_id)
    add_participant_override(session, season.id, golfer_id=golfer.id, seed_handicap_strokes="12")
    from golf_league.services.roster import RosterValidationError
    with pytest.raises(RosterValidationError, match="season participant"):
        delete_golfer(session, golfer.id)


def test_unreferenced_golfer_can_be_deleted(session, golfer):
    from golf_league.services.roster import get_golfer

    assert delete_golfer(session, golfer.id) is True
    assert get_golfer(session, golfer.id) is None


def test_admin_participant_page_and_add_require_admin_and_csrf(admin_client, empty_client):
    assert empty_client.get("/admin/seasons/1/participants").status_code == 401
    db = Session(bind=admin_client.app.state.engine)
    try:
        course = db.execute(select(Course)).scalars().first()
        tee = db.execute(select(TeeSet).where(TeeSet.course_id == course.id)).scalars().first()
        season = _season(db, course.id)
        golfer = _golfer(db, tee)
        ids = season.id, golfer.id
    finally:
        db.close()
    page = admin_client.get(f"/admin/seasons/{ids[0]}/participants")
    assert page.status_code == 200
    token = _extract_csrf(page.text)
    assert "Every active golfer without a team is a sub" in page.text
    assert admin_client.post(
        f"/admin/seasons/{ids[0]}/participants/add",
        data={"golfer_id": str(ids[1]), "seed_handicap_strokes": "-3", "csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    assert "No handicap at this tee" not in admin_client.get(page.url).text
    duplicate_page = admin_client.get(f"/admin/seasons/{ids[0]}/participants")
    duplicate = admin_client.post(
        f"/admin/seasons/{ids[0]}/participants/add",
        data={
            "golfer_id": str(ids[1]),
            "seed_handicap_strokes": "-3",
            "csrf_token": _extract_csrf(duplicate_page.text),
        },
    )
    assert duplicate.status_code == 422
    assert "already has an override row" in duplicate.text
    assert admin_client.post(
        f"/admin/seasons/{ids[0]}/participants/add",
        data={"golfer_id": str(ids[1]), "seed_handicap_strokes": "0"},
    ).status_code == 403


def test_admin_golfer_delete_reference_check_uses_409_and_csrf(admin_client):
    db = Session(bind=admin_client.app.state.engine)
    try:
        tee = db.execute(select(TeeSet)).scalars().first()
        golfer = _golfer(db, tee)
        season = _season(db, golfer.default_tee_set.course_id)
        add_participant_override(db, season.id, golfer_id=golfer.id, seed_handicap_strokes="3")
        golfer_id = golfer.id
    finally:
        db.close()
    page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    response = admin_client.post(
        f"/admin/golfers/{golfer_id}/delete",
        data={"csrf_token": _extract_csrf(page.text)}, follow_redirects=False,
    )
    assert response.status_code == 409
    assert "season participant row" in response.text


def test_admin_cross_course_override_rerenders_422(admin_client):
    db = Session(bind=admin_client.app.state.engine)
    try:
        course = db.execute(select(Course)).scalars().first()
        tee = db.execute(select(TeeSet).where(TeeSet.course_id == course.id)).scalars().first()
        season = _season(db, course.id)
        golfer = _golfer(db, tee)
        other = Course(name="Other Admin Course", total_holes=18)
        db.add(other)
        db.flush()
        foreign = TeeSet(course_id=other.id, name="Away", color_label="Away", gender="men", total_yards=1000, sort_order=0)
        db.add(foreign)
        db.commit()
        season_id, golfer_id, foreign_id = season.id, golfer.id, foreign.id
    finally:
        db.close()
    page = admin_client.get(f"/admin/seasons/{season_id}/participants")
    response = admin_client.post(
        f"/admin/seasons/{season_id}/participants/add",
        data={"golfer_id": str(golfer_id), "tee_set_id": str(foreign_id), "csrf_token": _extract_csrf(page.text)},
    )
    assert response.status_code == 422
    assert "Choose a tee at this season&#39;s course." in response.text
