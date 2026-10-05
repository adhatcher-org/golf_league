"""GL-32 team membership, position and admin route acceptance tests."""

import re
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from golf_league.domain.roster import role_from_membership
from golf_league.models import (
    Course,
    Golfer,
    Season,
    SeasonGolfer,
    SeasonParticipant,
    Team,
    TeamMember,
    TeeSet,
    User,
)
from golf_league.services.auth import hash_password
from golf_league.services.roster import (
    RosterConflictError,
    RosterValidationError,
    delete_golfer,
    update_golfer,
)
from golf_league.services.teams import (
    TeamValidationError,
    assign_positions_by_handicap,
    list_season_teams,
    save_team,
    season_matchups_ready,
)


def _season(session, course_id, *, status="draft"):
    season = Season(
        year=2026, course_id=course_id, start_date=date(2026, 8, 27),
        end_date=date(2026, 10, 29), play_weekday=3, status=status,
        first_week_nine="front",
    )
    session.add(season)
    session.commit()
    return season


def _golfer(session, tee, *, strokes=6, active=True, first="Sample"):
    golfer = Golfer(
        first_name=first, last_name="Golfer", default_tee_set_id=tee.id,
        default_tee_label=tee.color_label,
        handicap_strokes=strokes, handicap_source="self_reported",
        handicap_status="needs_contact" if strokes is None else "ok",
        is_active=active,
    )
    session.add(golfer)
    session.commit()
    return golfer


def _member(golfer, position):
    return {"golfer_id": golfer.id, "position": position}


def _team(session, season, golfers, *, number=1, name="Sample Team"):
    existing_ids = set(session.scalars(
        select(SeasonGolfer.golfer_id).where(SeasonGolfer.season_id == season.id)
    ))
    session.add_all(
        SeasonGolfer(season_id=season.id, golfer_id=golfer.id)
        for golfer in golfers
        if golfer.is_active and golfer.id not in existing_ids
    )
    session.commit()
    return save_team(
        session, season.id, name=name, number=number, sort_order=number,
        members=[_member(golfer, position) for position, golfer in enumerate(golfers, 1)],
    )


def _ids_for_client(client):
    session = Session(bind=client.app.state.engine)
    try:
        course = session.execute(select(Course)).scalars().first()
        tee = session.execute(select(TeeSet).where(TeeSet.course_id == course.id)).scalars().first()
        season = _season(session, course.id, status="active")
        golfer = _golfer(session, tee)
        session.add(SeasonGolfer(season_id=season.id, golfer_id=golfer.id))
        session.commit()
        return season.id, golfer.id, tee.id
    finally:
        session.close()


def _csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]*)"', html)
    assert match
    return match.group(1)


def test_team_member_role_is_derived_from_membership(session, wyandot_course):
    course = wyandot_course()
    tee = course.tee_sets[0]
    season = _season(session, course.id)
    golfer = _golfer(session, tee)
    assert not hasattr(golfer, "role")
    assert not hasattr(SeasonParticipant, "role")
    team = _team(session, season, [golfer])
    member = session.execute(
        select(TeamMember).where(TeamMember.team_id == team.id)
    ).scalar_one()
    assert member.golfer_id == golfer.id
    assert role_from_membership(member is not None) == "active"
    sub = _golfer(session, tee, first="Sub")
    sub_has_slot = session.execute(
        select(TeamMember.id).where(
            TeamMember.season_id == season.id, TeamMember.golfer_id == sub.id
        )
    ).first() is not None
    assert role_from_membership(sub_has_slot) == "sub"


def test_partial_team_can_be_saved_and_is_marked_incomplete(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    golfers = [_golfer(session, course.tee_sets[0], first=f"Golfer {i}") for i in range(10)]
    views = []
    offset = 0
    for size in range(1, 5):
        team = _team(session, season, golfers[offset:offset + size], number=size)
        offset += size
        views.append(next(view for view in list_season_teams(session, season.id) if view.team.id == team.id))
        assert views[-1].complete is (size == 4)
    with pytest.raises(TeamValidationError, match="one to four"):
        save_team(session, season.id, name="Empty", number=5, sort_order=5, members=[])


def test_team_mutation_is_atomic_on_invalid_member(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    first = _golfer(session, course.tee_sets[0], first="Original")
    replacement = _golfer(session, course.tee_sets[0], first="Replacement")
    no_seed = _golfer(session, course.tee_sets[0], strokes=None, first="Missing")
    team = _team(session, season, [first])
    session.add_all([
        SeasonGolfer(season_id=season.id, golfer_id=replacement.id),
        SeasonGolfer(season_id=season.id, golfer_id=no_seed.id),
    ])
    session.commit()
    with pytest.raises(TeamValidationError, match="effective seed"):
        save_team(
            session, season.id, team_id=team.id, name="Changed", number=9,
            sort_order=9, members=[_member(replacement, 1), _member(no_seed, 2)],
        )
    session.expire_all()
    saved = session.get(Team, team.id)
    assert (saved.name, saved.number, saved.sort_order) == ("Sample Team", 1, 1)
    member = session.execute(select(TeamMember).where(TeamMember.team_id == team.id)).scalar_one()
    assert (member.golfer_id, member.position) == (first.id, 1)


def test_members_must_be_active_course_eligible_and_have_effective_seed(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    inactive = _golfer(session, course.tee_sets[0], active=False, first="Inactive")
    no_seed = _golfer(session, course.tee_sets[0], strokes=None, first="NoSeed")
    valid = _golfer(session, course.tee_sets[0], first="Fallback")
    with pytest.raises(TeamValidationError, match="active golfer"):
        _team(session, season, [inactive])
    with pytest.raises(TeamValidationError, match="effective seed"):
        _team(session, season, [no_seed])
    assert _team(session, season, [valid]) is not None

    other_course = Course(name="Other Course", total_holes=18)
    session.add(other_course)
    session.flush()
    foreign_tee = TeeSet(
        course_id=other_course.id, name="Away", color_label="Away", gender="men",
        total_yards=5000, sort_order=0,
    )
    session.add(foreign_tee)
    session.flush()
    wrong_course = _golfer(session, foreign_tee, first="WrongCourse")
    with pytest.raises(TeamValidationError, match="effective tee"):
        _team(session, season, [wrong_course], number=2)

    other_season = _season(session, course.id)
    existing_team = _team(session, other_season, [valid], number=1)
    with pytest.raises(TeamValidationError, match="already assigned"):
        save_team(session, season.id, name="Tamper", number=3, sort_order=3, members=[_member(valid, 1)])
    assert existing_team.season_id == other_season.id
    assert save_team(session, season.id, team_id=existing_team.id, name="Tamper", number=3,
                     sort_order=3, members=[_member(valid, 1)]) is None

    alternate_tee = next(tee for tee in course.tee_sets if tee.id != valid.default_tee_set_id)
    override_golfer = _golfer(session, course.tee_sets[0], first="OverrideNoSeed")
    participant = SeasonParticipant(
        season_id=season.id, golfer_id=override_golfer.id, tee_set_id=alternate_tee.id,
        seed_handicap_strokes=None,
    )
    session.add(participant)
    session.add(SeasonGolfer(season_id=season.id, golfer_id=override_golfer.id))
    session.commit()
    with pytest.raises(TeamValidationError, match="effective seed"):
        save_team(session, season.id, name="NoFallback", number=4, sort_order=4,
                  members=[_member(override_golfer, 1)])


def test_golfer_can_join_only_one_team_per_season(session, wyandot_course):
    course = wyandot_course()
    season_one = _season(session, course.id)
    season_two = _season(session, course.id)
    golfer = _golfer(session, course.tee_sets[0])
    _team(session, season_one, [golfer])
    with pytest.raises(TeamValidationError, match="already assigned"):
        _team(session, season_one, [golfer], number=2)
    assert _team(session, season_two, [golfer]) is not None


def test_team_positions_are_unique_and_between_one_and_four(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    golfers = [_golfer(session, course.tee_sets[0], first=f"Position {i}") for i in range(2)]
    with pytest.raises(TeamValidationError, match="unique"):
        save_team(session, season.id, name="Duplicate position", number=1, sort_order=1,
                  members=[_member(golfers[0], 1), _member(golfers[1], 1)])
    with pytest.raises(TeamValidationError, match="between 1 and 4"):
        save_team(session, season.id, name="Outside", number=1, sort_order=1,
                  members=[_member(golfers[0], 5)])
    with pytest.raises(TeamValidationError, match="only once"):
        save_team(session, season.id, name="Duplicate golfer", number=1, sort_order=1,
                  members=[_member(golfers[0], 1), _member(golfers[0], 2)])


def test_assign_positions_uses_effective_seed_then_golfer_id(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    golfers = [_golfer(session, course.tee_sets[0], strokes=8, first=f"Tie {i}") for i in range(3)]
    golfers[1].handicap_strokes = -2
    session.commit()
    team = _team(session, season, golfers)
    assign_positions_by_handicap(session, team.id)
    result = list(session.execute(
        select(TeamMember).where(TeamMember.team_id == team.id).order_by(TeamMember.position)
    ).scalars())
    assert [member.golfer_id for member in result] == [golfers[1].id, golfers[0].id, golfers[2].id]
    assert [member.position for member in result] == [1, 2, 3]


def test_assign_positions_rejects_missing_seed_without_changing_positions(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    golfers = [_golfer(session, course.tee_sets[0], first=f"Seed {i}") for i in range(2)]
    team = _team(session, season, golfers)
    golfers[1].handicap_strokes = None
    session.commit()
    before = list(session.execute(
        select(TeamMember.golfer_id, TeamMember.position)
        .where(TeamMember.team_id == team.id).order_by(TeamMember.position)
    ))
    with pytest.raises(TeamValidationError, match="effective seed"):
        assign_positions_by_handicap(session, team.id)
    after = list(session.execute(
        select(TeamMember.golfer_id, TeamMember.position)
        .where(TeamMember.team_id == team.id).order_by(TeamMember.position)
    ))
    assert after == before


def test_position_override_survives_seed_change_until_explicit_assignment(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    golfers = [_golfer(session, course.tee_sets[0], strokes=value, first=f"Change {value}") for value in (3, 9)]
    team = _team(session, season, golfers)
    golfers[0].handicap_strokes = 20
    session.commit()
    positions = dict(session.execute(
        select(TeamMember.golfer_id, TeamMember.position).where(TeamMember.team_id == team.id)
    ).all())
    assert positions == {golfers[0].id: 1, golfers[1].id: 2}
    assign_positions_by_handicap(session, team.id)
    positions = dict(session.execute(
        select(TeamMember.golfer_id, TeamMember.position).where(TeamMember.team_id == team.id)
    ).all())
    assert positions == {golfers[0].id: 2, golfers[1].id: 1}


def test_matchup_readiness_requires_every_team_to_have_four_members(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    golfers = [_golfer(session, course.tee_sets[0], first=f"Ready {i}") for i in range(5)]
    assert season_matchups_ready(session, season.id) is False
    _team(session, season, golfers[:4])
    assert season_matchups_ready(session, season.id) is True
    _team(session, season, golfers[4:], number=2)
    assert season_matchups_ready(session, season.id) is False


def test_delete_golfer_refuses_team_membership_reference(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id)
    golfer = _golfer(session, course.tee_sets[0])
    team = _team(session, season, [golfer])
    session.add(User(
        username="linked@example.test", email="linked@example.test", display_name="Linked",
        password_hash="x", golfer_id=golfer.id,
    ))
    session.add(SeasonParticipant(season_id=season.id, golfer_id=golfer.id,
                                  seed_handicap_strokes=0))
    session.commit()
    with pytest.raises(RosterValidationError) as error:
        delete_golfer(session, golfer.id)
    text = str(error.value.errors)
    assert "user account" in text
    assert "season participant row" in text
    assert "team membership row" in text
    assert session.get(Team, team.id) is not None


def test_deactivation_of_active_season_member_is_refused(session, wyandot_course):
    course = wyandot_course()
    season = _season(session, course.id, status="active")
    golfer = _golfer(session, course.tee_sets[0])
    team = _team(session, season, [golfer])
    values = {
        "first_name": golfer.first_name,
        "last_name": golfer.last_name,
        "default_tee_set_id": golfer.default_tee_set_id,
        "email": golfer.email,
        "handicap_strokes": golfer.handicap_strokes,
        "is_active": "false",
    }
    with pytest.raises(RosterConflictError, match=team.name):
        update_golfer(session, golfer.id, **values)
    assert session.get(Golfer, golfer.id).is_active is True
    season.status = "draft"
    session.commit()
    updated = update_golfer(session, golfer.id, **values)
    assert updated.is_active is False


def test_admin_team_routes_require_admin_csrf_and_status_contracts(admin_client, empty_client):
    season_id, golfer_id, tee_id = _ids_for_client(admin_client)
    assert empty_client.get(f"/admin/seasons/{season_id}/teams").status_code == 401
    page = admin_client.get(f"/admin/seasons/{season_id}/teams/new")
    assert page.status_code == 200
    count_before = Session(bind=admin_client.app.state.engine)
    try:
        assert count_before.scalar(select(func.count()).select_from(Team)) == 0
    finally:
        count_before.close()
    missing_csrf = admin_client.post(
        f"/admin/seasons/{season_id}/teams/new",
        data={"name": "No token", "number": "1", "sort_order": "1", "member_1": str(golfer_id)},
    )
    assert missing_csrf.status_code == 403
    invalid = admin_client.post(
        f"/admin/seasons/{season_id}/teams/new",
        data={"name": "", "number": "1", "sort_order": "0", "member_1": str(golfer_id),
              "csrf_token": _csrf(page.text)},
    )
    assert invalid.status_code == 422
    created = admin_client.post(
        f"/admin/seasons/{season_id}/teams/new",
        data={"name": "Route Team", "number": "1", "sort_order": "0",
              "member_1": str(golfer_id), "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert created.status_code == 303
    session = Session(bind=admin_client.app.state.engine)
    try:
        team_id = session.scalar(select(Team.id))
    finally:
        session.close()
    edit_page = admin_client.get(f"/admin/teams/{team_id}/edit")
    assert edit_page.status_code == 200
    edited = admin_client.post(
        f"/admin/teams/{team_id}/edit",
        data={"name": "Edited Route Team", "number": "1", "sort_order": "2",
              "member_1": str(golfer_id), "csrf_token": _csrf(edit_page.text)},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    edit_page = admin_client.get(f"/admin/teams/{team_id}/edit")
    assigned = admin_client.post(
        f"/admin/teams/{team_id}/assign-positions",
        data={"csrf_token": _csrf(edit_page.text)}, follow_redirects=False,
    )
    assert assigned.status_code == 303
    assert admin_client.get("/admin/teams/999999/edit").status_code == 404
    missing_edit = admin_client.post(
        "/admin/teams/999999/edit", data={"csrf_token": _csrf(edit_page.text)}
    )
    assert missing_edit.status_code == 404
    session = Session(bind=admin_client.app.state.engine)
    try:
        session.get(Golfer, golfer_id).handicap_strokes = None
        session.commit()
    finally:
        session.close()
    edit_page = admin_client.get(f"/admin/teams/{team_id}/edit")
    missing_seed = admin_client.post(
        f"/admin/teams/{team_id}/assign-positions",
        data={"csrf_token": _csrf(edit_page.text)},
    )
    assert missing_seed.status_code == 422
    golfer_page = admin_client.get(f"/admin/golfers/{golfer_id}/edit")
    delete_refused = admin_client.post(
        f"/admin/golfers/{golfer_id}/delete",
        data={"csrf_token": _csrf(golfer_page.text)}, follow_redirects=False,
    )
    assert delete_refused.status_code == 409
    assert "team membership row" in delete_refused.text
    deactivate_refused = admin_client.post(
        f"/admin/golfers/{golfer_id}/edit",
        data={
            "first_name": "Sample", "last_name": "Golfer", "email": "", "phone": "",
            "default_tee_set_id": str(tee_id),
            "handicap_strokes": "", "notes": "", "is_active": "false",
            "rendered_tee_set_id": str(tee_id), "csrf_token": _csrf(golfer_page.text),
        },
    )
    assert deactivate_refused.status_code == 409
    assert "Edited Route Team" in deactivate_refused.text
    after = Session(bind=admin_client.app.state.engine)
    try:
        assert after.scalar(select(func.count()).select_from(Team)) == 1
        assert after.get(Team, team_id).name == "Edited Route Team"
    finally:
        after.close()
    session = Session(bind=empty_client.app.state.engine)
    try:
        email = "member@example.test"
        session.add(User(
            username=email, email=email, display_name="Member",
            password_hash=hash_password("test-password"), is_admin=False,
            email_verified_at=datetime.now(UTC).replace(tzinfo=None),
        ))
        session.commit()
    finally:
        session.close()
    login_page = empty_client.get("/login")
    logged_in = empty_client.post(
        "/login",
        data={"email": email, "password": "test-password", "csrf_token": _csrf(login_page.text)},
        follow_redirects=False,
    )
    assert logged_in.status_code == 303
    assert empty_client.get("/admin/seasons/1/teams").status_code == 403


def test_empty_string_is_active_form_value_keeps_existing_status(session, wyandot_course):
    course = wyandot_course()
    golfer = _golfer(session, course.tee_sets[0])
    updated = update_golfer(
        session, golfer.id, first_name=golfer.first_name, last_name=golfer.last_name,
        default_tee_set_id=golfer.default_tee_set_id, handicap_strokes=golfer.handicap_strokes,
        is_active="",
    )
    assert updated.is_active is True
