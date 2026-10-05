"""Nine edit, optional rotation and complete roster par warnings."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import matchup_db as _matchup_db
from test_week_shift import confirm_data, hidden
from test_week_shift import fall as _fall
from test_week_shift import route_schedule as _route_schedule

from golf_league.domain.schedule import rotate_nines
from golf_league.models import Golfer, SeasonGolfer, SeasonParticipant, TeeRating, Week
from golf_league.services.schedule import (
    ScheduleConflict,
    ScheduleValidationError,
    confirm_nine,
    confirm_nine_core,
    list_weeks,
    preview_nine,
)

matchup_db = _matchup_db
fall = _fall
route_schedule = _route_schedule

def test_single_nine_edit_preserves_other_weeks_and_all_metadata(session, fall):
    season, weeks = fall
    before = [(w.id, w.index, w.play_date, w.nine, w.week_type, w.status, w.makeup_for_week_id, w.notes) for w in weeks]
    preview = preview_nine(session, season.id, weeks[3].id, new_nine="front")
    assert [c.index for c in preview.changes] == [4]
    confirm_nine(session, season.id, weeks[3].id, new_nine="front", expected_fingerprint=preview.fingerprint)
    assert [(w.id, w.index, w.play_date, w.nine, w.week_type, w.status, w.makeup_for_week_id, w.notes) for w in list_weeks(session, season.id)] == [(*row[:3], "front" if row[1] == 4 else row[3], *row[4:]) for row in before]


def test_rerotate_preview_skips_null_cancelled_played_and_writes_nothing(session, fall):
    season, weeks = fall
    weeks[4].status = "played"
    weeks[5].status = "cancelled"
    session.commit()
    before = [w.nine for w in weeks]
    preview = preview_nine(session, season.id, weeks[3].id, new_nine="front", rerotate=True)
    assert [(c.index, c.new_value) for c in preview.changes] == [(4, "front"), (7, "back"), (8, "front"), (9, "back")]
    assert [w.nine for w in list_weeks(session, season.id)] == before
    assert not session.dirty
    confirm_nine(session, season.id, weeks[3].id, new_nine="front", rerotate=True, expected_fingerprint=preview.fingerprint)
    assert weeks[4].nine == before[4] and weeks[5].nine == before[5]
    assert weeks[9].nine is None
    assert weeks[3].play_date == date(2026, 9, 17)


def test_nine_refusals_and_invalid_nine(session, fall):
    season, weeks = fall
    with pytest.raises(ScheduleValidationError, match="GL-37"):
        preview_nine(session, season.id, weeks[9].id, new_nine="front")
    with pytest.raises(ScheduleValidationError):
        preview_nine(session, season.id, weeks[3].id, new_nine="full")
    weeks[3].status = "played"
    session.commit()
    with pytest.raises(ScheduleConflict, match="Played"):
        preview_nine(session, season.id, weeks[3].id, new_nine="front")
    with pytest.raises(LookupError):
        preview_nine(session, 99999, weeks[2].id, new_nine="back")


def test_pure_rotation_null_does_not_advance_and_earlier_omitted():
    assert rotate_nines([(1, "back"), (2, "back"), (3, None), (4, "front"), (5, "back")], 2, "front") == [(2, "front"), (3, None), (4, "back"), (5, "front")]
    for rows, index, nine in [([], 1, "front"), ([(1, None)], 1, "front"), ([(1, "front")], 1, "invalid"), ([(1, "front"), (2, "full")], 1, "back")]:
        with pytest.raises(ValueError):
            rotate_nines(rows, index, nine)


def test_par_warnings_complete_roster_overrides_and_later_changes(session, fall, golfer):
    season, weeks = fall
    session.add(SeasonGolfer(season_id=season.id, golfer_id=golfer.id))
    second = Golfer(first_name="Casey", last_name="Example", default_tee_set_id=golfer.default_tee_set_id,
                    handicap_strokes=0, handicap_status="ok", handicap_source="self_reported")
    session.add(second)
    session.flush()
    session.add(SeasonGolfer(season_id=season.id, golfer_id=second.id))
    session.add(SeasonParticipant(season_id=season.id, golfer_id=second.id, tee_set_id=golfer.default_tee_set_id, seed_handicap_strokes=-1))
    back = session.scalar(select(TeeRating).where(TeeRating.tee_set_id == golfer.default_tee_set_id, TeeRating.scope == "back"))
    back.par = 35
    session.commit()
    preview = preview_nine(session, season.id, weeks[3].id, new_nine="front", rerotate=True)
    assert len(preview.warnings) == 12  # weeks 4–9, both selected golfers, no team needed.
    assert {w.golfer_id for w in preview.warnings} == {golfer.id, second.id}
    assert {w.index for w in preview.warnings} == set(range(4, 10))
    assert {(w.old_par, w.new_par) for w in preview.warnings} == {(35, 36), (36, 35)}
    confirm_nine(session, season.id, weeks[3].id, new_nine="front", rerotate=True, expected_fingerprint=preview.fingerprint)
    assert golfer.handicap_strokes == 12
    assert second.handicap_strokes == 0


def test_equal_par_missing_rating_and_unresolved_tee_omit_warnings(session, fall, golfer):
    season, weeks = fall
    session.add(SeasonGolfer(season_id=season.id, golfer_id=golfer.id))
    session.commit()
    assert preview_nine(session, season.id, weeks[3].id, new_nine="front").warnings == ()
    session.delete(session.scalar(select(TeeRating).where(TeeRating.tee_set_id == golfer.default_tee_set_id, TeeRating.scope == "back")))
    session.commit()
    assert preview_nine(session, season.id, weeks[3].id, new_nine="front").warnings == ()
    golfer.default_tee_set_id = None
    golfer.default_tee_label = "Unavailable"
    session.commit()
    assert preview_nine(session, season.id, weeks[3].id, new_nine="front").warnings == ()


def test_nine_core_composes_with_caller_rollback(route_schedule):
    client, sid, ids = route_schedule
    with Session(client.app.state.engine) as session:
        preview = preview_nine(session, sid, ids[3], new_nine="front")
        result = confirm_nine_core(session, sid, ids[3], new_nine="front", expected_fingerprint=preview.fingerprint)
        assert result.changes == preview.changes and session.dirty
        session.flush()
        session.rollback()
        assert session.get(Week, ids[3]).nine == "back"


def test_nine_fingerprint_binds_target_rotation_and_all_rows(route_schedule):
    client, sid, ids = route_schedule
    with Session(client.app.state.engine) as session:
        preview = preview_nine(session, sid, ids[3], new_nine="front")
        with pytest.raises(ScheduleConflict):
            confirm_nine(session, sid, ids[3], new_nine="back", expected_fingerprint=preview.fingerprint)
        with pytest.raises(ScheduleConflict):
            confirm_nine(session, sid, ids[3], new_nine="front", rerotate=True, expected_fingerprint=preview.fingerprint)
        session.get(Week, ids[0]).notes = "Earlier changed row"
        session.commit()
        with pytest.raises(ScheduleConflict):
            confirm_nine(session, sid, ids[3], new_nine="front", expected_fingerprint=preview.fingerprint)
        assert session.get(Week, ids[3]).nine == "back"


def test_nine_http_preview_confirm_tamper_stale_and_refusals(route_schedule):
    client, sid, ids = route_schedule
    base = f"/admin/seasons/{sid}/weeks/{ids[3]}/nine"
    csrf = hidden(client.get(base).text, "csrf_token")
    assert client.post(base + "/preview", data={"csrf_token": csrf, "new_nine": "wrong"}).status_code == 422
    preview = client.post(base + "/preview", data={"csrf_token": csrf, "new_nine": "front", "rerotate": "1"})
    assert preview.status_code == 200
    data = confirm_data(preview.text, "new_nine")
    for changed in ({"new_nine": "back"}, {"rerotate": "0"}, {"fingerprint": "forged"}, {"signature": "forged"}):
        assert client.post(base + "/confirm", data={**data, **changed}).status_code == 409
    with Session(client.app.state.engine) as session:
        assert session.get(Week, ids[3]).nine == "back"
        session.get(Week, ids[0]).notes = "Concurrent edit"
        session.commit()
    assert client.post(base + "/confirm", data=data).status_code == 409
    preview = client.post(base + "/preview", data={"csrf_token": csrf, "new_nine": "front", "rerotate": "1"})
    assert client.post(base + "/confirm", data=confirm_data(preview.text, "new_nine"), follow_redirects=False).status_code == 303
    rain = base.replace(f"/weeks/{ids[3]}/", f"/weeks/{ids[9]}/")
    assert client.post(rain + "/preview", data={"csrf_token": csrf, "new_nine": "front"}).status_code == 422
    with Session(client.app.state.engine) as session:
        session.get(Week, ids[3]).status = "played"
        session.commit()
    assert client.post(base + "/preview", data={"csrf_token": csrf, "new_nine": "back"}).status_code == 409


def test_rotation_warnings_snapshot_only_persisted_tees_and_relabel(matchup_db):
    from test_matchup_generation import clock

    from golf_league.models import WeekHandicap
    from golf_league.services.handicaps import ensure_week_handicap
    from golf_league.services.matchups import generate_week_matches

    engine, ids = matchup_db
    with Session(engine) as session:
        for week in ids['weeks'][:2]:
            generate_week_matches(session, ids['season'], week_id=week,
                home_team_id=ids['teams'][0], away_team_id=ids['teams'][1], clock=clock)
        sub = Golfer(first_name='Unselected', last_name='Synthetic', default_tee_set_id=ids['tee'],
                     handicap_strokes=0, handicap_source='self_reported', handicap_status='ok')
        session.add(sub)
        session.commit()
        sub_id = sub.id
        for week in ids['weeks'][:2]:
            ensure_week_handicap(session, week, sub_id, clock=clock)
        session.commit()
        sub.default_tee_set_id = None
        sub.default_tee_label = 'Unavailable'
        rating = session.scalar(select(TeeRating).where(TeeRating.tee_set_id == ids['tee'], TeeRating.scope == 'back'))
        rating.par = 35
        session.commit()
        before = {r.id:(r.strokes, r.source, r.computed_at) for r in session.scalars(select(WeekHandicap))}
        preview = preview_nine(session, ids['season'], ids['weeks'][0], new_nine='back', rerotate=True)
        assert len(preview.warnings) == 50
        assert {w.index for w in preview.warnings} == {1, 2, 3}
        assert {(w.week_id, w.golfer_id, w.tee_set_id) for w in preview.warnings}.__len__() == 50
        assert len([w for w in preview.warnings if w.golfer_id == sub_id]) == 2
        confirm_nine(session, ids['season'], ids['weeks'][0], new_nine='back', rerotate=True,
                     expected_fingerprint=preview.fingerprint)
        for snap in session.scalars(select(WeekHandicap)):
            assert snap.nine == session.get(Week, snap.week_id).nine
            assert (snap.strokes, snap.source, snap.computed_at) == before[snap.id]
