"""Cascade acceptance and signed admin form boundaries, using synthetic rows."""

import re
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from golf_league.domain.schedule import shift_from
from golf_league.models import Course, Season, User, Week
from golf_league.services.schedule import (
    ScheduleConflict,
    ScheduleValidationError,
    build_default_proposal,
    commit_generated_weeks,
    confirm_shift,
    list_weeks,
    preview_shift,
    schedule_fingerprint,
)
from golf_league.services.seasons import create_season


def make_fall(session, course):
    season = create_season(session, year=2026, name_override="", course_id=course.id,
                           start_date="2026-08-27", end_date="2026-10-29", play_weekday=3,
                           status="draft", first_week_nine="front")
    proposal = build_default_proposal(season, 10)
    for index in (1, 6):
        proposal[index - 1] = replace(proposal[index - 1], week_type="play_with_team")
    proposal[0] = replace(proposal[0], status="cancelled")
    proposal[9] = replace(proposal[9], week_type="rain_date", nine=None, makeup_for_index=9)
    weeks = commit_generated_weeks(session, season.id, proposal=proposal,
                                   expected_fingerprint=schedule_fingerprint(season))
    return season, weeks


@pytest.fixture
def fall(session, wyandot_course):
    return make_fall(session, wyandot_course())


@pytest.fixture
def route_schedule(admin_client):
    with Session(admin_client.app.state.engine) as session:
        course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
        season, weeks = make_fall(session, course)
        return admin_client, season.id, [week.id for week in weeks]


def hidden(html, name):
    return re.search(r'name="' + name + r'" value="([^"]*)"', html).group(1)


def confirm_data(html, field):
    return {name: hidden(html, name) for name in ("csrf_token", "fingerprint", "signature", "rerotate", field)}


def test_fall_shift_preview_atomic_confirm_and_metadata(session, fall):
    season, weeks = fall
    old = [(w.id, w.index, w.play_date, w.nine, w.week_type, w.status, w.makeup_for_week_id) for w in weeks]
    preview = preview_shift(session, season.id, weeks[3].id, new_date=date(2026, 9, 24))
    assert [c.index for c in preview.changes] == list(range(4, 11))
    assert [(w.id, w.index, w.play_date, w.nine, w.week_type, w.status, w.makeup_for_week_id) for w in list_weeks(session, season.id)] == old
    assert not session.dirty
    confirm_shift(session, season.id, weeks[3].id, new_date=date(2026, 9, 24), expected_fingerprint=preview.fingerprint)
    result = list_weeks(session, season.id)
    assert [w.play_date for w in result] == [row[2] + timedelta(days=7 if row[1] >= 4 else 0) for row in old]
    assert [(w.id, w.index, w.nine, w.week_type, w.status, w.makeup_for_week_id) for w in result] == [(r[0], r[1], *r[3:]) for r in old]
    assert session.get(Season, season.id).end_date == date(2026, 11, 5)


def test_zero_delta_has_no_business_writes(session, fall):
    season, weeks = fall
    preview = preview_shift(session, season.id, weeks[3].id, new_date=weeks[3].play_date)
    statements = []
    connection = session.connection()
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(connection, "before_cursor_execute", capture)
    confirm_shift(session, season.id, weeks[3].id, new_date=weeks[3].play_date, expected_fingerprint=preview.fingerprint)
    event.remove(connection, "before_cursor_execute", capture)
    assert preview.changes == ()
    assert not any(s.lstrip().upper().startswith(("INSERT", "DELETE")) or
                   (s.lstrip().upper().startswith("UPDATE") and "WHERE 0" not in s) for s in statements)


def test_shift_rejections_preserve_every_row(session, fall):
    season, weeks = fall
    before = [w.play_date for w in weeks]
    with pytest.raises(ScheduleValidationError):
        preview_shift(session, season.id, weeks[3].id, new_date=date(2026, 9, 20))
    with pytest.raises(ScheduleConflict, match="collides"):
        preview_shift(session, season.id, weeks[3].id, new_date=weeks[2].play_date)
    weeks[4].status = "played"
    session.commit()
    with pytest.raises(ScheduleConflict, match="Played"):
        preview_shift(session, season.id, weeks[3].id, new_date=date(2026, 9, 24))
    assert [w.play_date for w in list_weeks(session, season.id)] == before


@pytest.mark.parametrize("new_date", ["invalid", datetime(2026, 9, 24)])
def test_domain_rejects_non_calendar_dates(new_date):
    with pytest.raises(ValueError):
        shift_from([(1, date(2026, 9, 17), "scheduled")], 1, new_date)


def test_domain_shift_missing_overflow_and_played():
    with pytest.raises(ValueError, match="does not exist"):
        shift_from([], 1, date(2026, 9, 24))
    with pytest.raises(ValueError, match="calendar"):
        shift_from([(1, "bad", "scheduled")], 1, date(2026, 9, 24))
    with pytest.raises(ValueError, match="range"):
        shift_from([(1, date(9999, 12, 24), "scheduled"), (2, date(9999, 12, 31), "scheduled")], 1, date(9999, 12, 31))
    with pytest.raises(ValueError, match="Played"):
        shift_from([(1, date(2026, 9, 17), "played")], 1, date(2026, 9, 24))


def test_shift_http_preview_confirm_validation_tampering_and_stale(route_schedule):
    client, sid, ids = route_schedule
    path = f"/admin/seasons/{sid}/weeks/{ids[3]}/shift"
    form = client.get(path)
    assert form.status_code == 200
    csrf = hidden(form.text, "csrf_token")
    for bad in ("", "2026-09-20", "not-a-date"):
        response = client.post(path + "/preview", data={"csrf_token": csrf, "new_date": bad})
        assert response.status_code == 422 and "text/html" in response.headers["content-type"]
    preview = client.post(path + "/preview", data={"csrf_token": csrf, "new_date": "2026-09-24"})
    assert preview.status_code == 200
    data = confirm_data(preview.text, "new_date")
    assert client.post(path + "/confirm", data={**data, "new_date": "2026-10-01"}).status_code == 409
    with Session(client.app.state.engine) as session:
        assert session.get(Week, ids[3]).play_date == date(2026, 9, 17)
        session.get(Week, ids[0]).notes = "Synthetic concurrent change"
        session.commit()
    assert client.post(path + "/confirm", data=data).status_code == 409
    preview = client.post(path + "/preview", data={"csrf_token": csrf, "new_date": "2026-09-24"})
    assert client.post(path + "/confirm", data=confirm_data(preview.text, "new_date"), follow_redirects=False).status_code == 303
    with Session(client.app.state.engine) as session:
        session.get(Week, ids[4]).status = "played"
        session.commit()
    assert client.post(path + "/preview", data={"csrf_token": csrf, "new_date": "2026-10-01"}).status_code == 409


@pytest.mark.parametrize("action", ["shift", "nine"])
def test_change_routes_resource_csrf_and_role_boundaries(route_schedule, action):
    client, sid, ids = route_schedule
    base = f"/admin/seasons/{sid}/weeks/{ids[3]}/{action}"
    csrf = hidden(client.get(base).text, "csrf_token")
    field = "new_date" if action == "shift" else "new_nine"
    target = "2026-09-24" if action == "shift" else "front"
    data = {"csrf_token": csrf, field: target}
    for endpoint in ("preview", "confirm"):
        assert client.post(base + "/" + endpoint, data={field: target}).status_code == 403
        assert client.post(base + "/" + endpoint, data={**data, "csrf_token": "invalid"}).status_code == 403
        assert client.post(base.replace(f"/seasons/{sid}/", "/seasons/99999/") + "/" + endpoint, data=data).status_code == 404
        assert client.post(base.replace(f"/weeks/{ids[3]}/", "/weeks/99999/") + "/" + endpoint, data=data).status_code == 404
    assert client.get(base.replace(f"/seasons/{sid}/", "/seasons/99999/")).status_code == 404
    with Session(client.app.state.engine) as session:
        course = session.scalar(select(Course))
        other, _ = make_fall(session, course)
        other_id = other.id
    assert client.post(base.replace(f"/seasons/{sid}/", f"/seasons/{other_id}/") + "/preview", data=data).status_code == 404
    with Session(client.app.state.engine) as session:
        admin = session.scalar(select(User).where(User.email == "admin@example.test"))
        admin.is_admin = False
        session.commit()
    assert client.get(base).status_code == 403
    for endpoint in ("preview", "confirm"):
        assert client.post(base + "/" + endpoint, data=data).status_code == 403
    with Session(client.app.state.engine) as session:
        admin = session.scalar(select(User).where(User.email == "admin@example.test"))
        admin.is_admin = True
        admin.email_verified_at = None
        session.commit()
    assert client.get(base).status_code == 403
    for endpoint in ("preview", "confirm"):
        assert client.post(base + "/" + endpoint, data=data).status_code == 403
    client.cookies.clear()
    assert client.get(base).status_code == 401
    for endpoint in ("preview", "confirm"):
        assert client.post(base + "/" + endpoint, data=data).status_code == 401


def test_confirmation_bypasses_cached_rows_and_detects_added_removed_weeks(route_schedule):
    client, sid, ids = route_schedule
    engine = client.app.state.engine
    with Session(engine) as cached, Session(engine) as writer:
        selected = cached.get(Week, ids[3])
        preview = preview_shift(cached, sid, selected.id, new_date=date(2026, 9, 24))
        changed = writer.get(Week, ids[0])
        changed.notes = "Independent connection changed earlier week"
        writer.commit()
        with pytest.raises(ScheduleConflict, match="fresh preview"):
            confirm_shift(cached, sid, selected.id, new_date=date(2026, 9, 24), expected_fingerprint=preview.fingerprint)
        assert cached.get(Week, ids[3]).play_date == date(2026, 9, 17)
        preview = preview_shift(cached, sid, ids[3], new_date=date(2026, 9, 24))
        writer.delete(writer.get(Week, ids[9]))
        writer.commit()
        with pytest.raises(ScheduleConflict):
            confirm_shift(cached, sid, ids[3], new_date=date(2026, 9, 24), expected_fingerprint=preview.fingerprint)


def test_confirmation_reserves_write_before_validation_and_refuses_competing_writer(route_schedule):
    client, sid, ids = route_schedule
    engine = client.app.state.engine
    with Session(engine) as confirming, Session(engine) as writer:
        preview = preview_shift(confirming, sid, ids[3], new_date=date(2026, 9, 24))
        confirming.connection().exec_driver_sql("PRAGMA busy_timeout = 1")
        writer.connection().exec_driver_sql("BEGIN IMMEDIATE")
        writer.connection().exec_driver_sql("UPDATE weeks SET notes = 'Synthetic writer' WHERE id = ?", (ids[0],))
        with pytest.raises(ScheduleConflict, match="Retry"):
            confirm_shift(confirming, sid, ids[3], new_date=date(2026, 9, 24), expected_fingerprint=preview.fingerprint)
        writer.commit()
        assert confirming.get(Week, ids[3]).play_date == date(2026, 9, 17)
        with pytest.raises(ScheduleConflict, match="fresh preview"):
            confirm_shift(confirming, sid, ids[3], new_date=date(2026, 9, 24), expected_fingerprint=preview.fingerprint)


@pytest.mark.parametrize("action,field,target,tampered", [
    ("shift", "new_date", "2026-09-24", "2026-10-01"),
    ("nine", "new_nine", "front", "back"),
])
def test_default_app_factory_signs_previews_and_refuses_tampered_confirmation(
    tmp_path, monkeypatch, action, field, target, tampered,
):
    from conftest import _make_admin_client
    from fastapi.testclient import TestClient

    from golf_league.app import create_app

    monkeypatch.setenv("SESSION_SECRET", "synthetic-default-factory-secret")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'default-factory.db'}")
    monkeypatch.setenv("SEED_COURSE", "true")
    app = create_app()
    assert not hasattr(app.state, "settings")
    with TestClient(app) as client:
        _make_admin_client(client)
        with Session(app.state.engine) as session:
            course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
            season, weeks = make_fall(session, course)
            sid, wid = season.id, weeks[3].id
        path = f"/admin/seasons/{sid}/weeks/{wid}/{action}"
        csrf = hidden(client.get(path).text, "csrf_token")
        preview = client.post(path + "/preview", data={"csrf_token": csrf, field: target})
        assert preview.status_code == 200
        data = confirm_data(preview.text, field)
        for altered in ({field: tampered}, {"signature": "forged"}):
            assert client.post(path + "/confirm", data={**data, **altered}).status_code == 409
        with Session(app.state.engine) as session:
            week = session.get(Week, wid)
            assert week.play_date == date(2026, 9, 17) and week.nine == "back"
        confirmed = client.post(path + "/confirm", data=data, follow_redirects=False)
        assert confirmed.status_code == 303
        assert confirmed.headers["location"] == f"/admin/seasons/{sid}/weeks"
        with Session(app.state.engine) as session:
            week = session.get(Week, wid)
            assert week.play_date == (date(2026, 9, 24) if action == "shift" else date(2026, 9, 17))
            assert week.nine == ("front" if action == "nine" else "back")
