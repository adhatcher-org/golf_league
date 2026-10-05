"""GL-37 serialized rain-date activation and cancellation contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
from conftest import _extract_csrf
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from golf_league.database import make_engine
from golf_league.models import (
    Base,
    Course,
    Golfer,
    PlayerMatch,
    Season,
    Team,
    TeamMatch,
    TeeSet,
    Week,
    WeekHandicap,
)
from golf_league.services.course_seed import seed_wyandot
from golf_league.services.schedule import (
    ScheduleConflict,
    ScheduleValidationError,
    confirm_rain_date,
    preview_rain_date,
)

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def _create_case(session, *, pairings=4, snapshot_count=8):
    seed_wyandot(session)
    course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
    tee = session.scalar(select(TeeSet).where(TeeSet.course_id == course.id, TeeSet.name == "Deer"))
    season = Season(year=2026, course_id=course.id, start_date=date(2026, 8, 27),
                    end_date=date(2026, 10, 29), play_weekday=3, status="draft",
                    first_week_nine="front")
    session.add(season)
    session.flush()
    weeks = []
    for index in range(1, 11):
        week = Week(season_id=season.id, index=index,
                    play_date=date(2026, 8, 27) + timedelta(days=7 * (index - 1)),
                    nine=None if index == 10 else ("front" if index % 2 else "back"),
                    week_type="rain_date" if index == 10 else "match", status="scheduled")
        session.add(week)
        weeks.append(week)
    session.flush()
    weeks[9].makeup_for_week_id = weeks[8].id
    teams = [Team(season_id=season.id, name=f"Team {i}", number=i, sort_order=i) for i in range(1, 9)]
    session.add_all(teams)
    golfers = [Golfer(first_name=f"Player{i}", last_name="Example", default_tee_set_id=tee.id,
                      handicap_strokes=i % 7, handicap_source="self_reported", handicap_status="ok")
               for i in range(1, 33)]
    session.add_all(golfers)
    session.flush()
    pairs = []
    players = []
    for index in range(pairings):
        match = TeamMatch(week_id=weeks[8].id, home_team_id=teams[index * 2].id,
                          away_team_id=teams[index * 2 + 1].id, is_self_match=False,
                          sort_order=index + 1)
        session.add(match)
        session.flush()
        pairs.append(match)
        for slot in range(4):
            row = PlayerMatch(team_match_id=match.id, position_label=str(slot + 1),
                              a_golfer_id=golfers[index * 8 + slot].id,
                              b_golfer_id=golfers[index * 8 + slot + 4].id,
                              manually_adjusted=(index == 0 and slot == 0),
                              a_is_sub=(index == 0 and slot == 0), generated_at=NOW)
            session.add(row)
            players.append(row)
    snapshots = []
    for golfer in golfers[:snapshot_count]:
        snapshot = WeekHandicap(week_id=weeks[8].id, golfer_id=golfer.id, tee_set_id=tee.id,
                                nine="front", strokes=golfer.handicap_strokes, source="carried",
                                computed_at=NOW)
        session.add(snapshot)
        snapshots.append(snapshot)
    session.flush()
    return {"season": season.id, "weeks": weeks, "pairs": pairs, "players": players,
            "snapshots": snapshots, "golfers": golfers, "tee": tee.id}


def _activate(session, ids):
    source, target = ids["weeks"][8], ids["weeks"][9]
    preview = preview_rain_date(session, ids["season"], source.id, target.id)
    return confirm_rain_date(session, ids["season"], source.id, target.id,
                              expected_fingerprint=preview.fingerprint)


def test_fall_week_nine_moves_matchups_and_snapshots_without_changing_identity(session):
    ids = _create_case(session)
    dates = [(row.id, row.index, row.play_date) for row in ids["weeks"]]
    pair_ids = [row.id for row in ids["pairs"]]
    player_values = [(row.id, row.team_match_id, row.manually_adjusted, row.a_is_sub,
                      row.generated_at) for row in ids["players"]]
    snapshot_values = [(row.id, row.golfer_id, row.tee_set_id, row.strokes, row.source,
                        row.computed_at) for row in ids["snapshots"]]
    preview = _activate(session, ids)
    source, target = ids["weeks"][8], ids["weeks"][9]
    assert preview.match_count == 4 and preview.snapshot_count == 8
    assert (source.status, target.week_type, target.status, target.nine,
            target.makeup_for_week_id) == ("cancelled", "match", "scheduled", "front", source.id)
    assert [(row.id, row.index, row.play_date) for row in ids["weeks"]] == dates
    assert [row.id for row in session.scalars(select(TeamMatch).where(TeamMatch.week_id == target.id))] == pair_ids
    assert not list(session.scalars(select(TeamMatch).where(TeamMatch.week_id == source.id)))
    assert [(row.id, row.team_match_id, row.manually_adjusted, row.a_is_sub,
             row.generated_at) for row in session.scalars(select(PlayerMatch).order_by(PlayerMatch.id))] == player_values
    moved = list(session.scalars(select(WeekHandicap).where(WeekHandicap.week_id == target.id).order_by(WeekHandicap.id)))
    assert [(row.id, row.golfer_id, row.tee_set_id, row.strokes, row.source,
             row.computed_at) for row in moved] == snapshot_values
    assert all(row.nine == "front" for row in moved)
    before_ids = [(row.id, row.week_id) for row in moved]
    second = confirm_rain_date(session, ids["season"], source.id, target.id,
                               expected_fingerprint=preview.fingerprint)
    assert second.match_count == 4 and second.snapshot_count == 8
    assert [(row.id, row.week_id) for row in moved] == before_ids


def test_empty_source_can_activate_and_printed_week_one_is_unchanged(session):
    ids = _create_case(session, pairings=0, snapshot_count=0)
    first = ids["weeks"][0]
    first.status = "cancelled"
    session.flush()
    before = (first.id, first.index, first.play_date, first.week_type, first.status, first.nine)
    _activate(session, ids)
    assert (first.id, first.index, first.play_date, first.week_type, first.status, first.nine) == before
    assert ids["weeks"][9].week_type == "match"


def test_preview_is_read_only_and_confirm_refuses_stale_schedule(session):
    ids = _create_case(session, pairings=1, snapshot_count=0)
    source, target = ids["weeks"][8], ids["weeks"][9]
    preview = preview_rain_date(session, ids["season"], source.id, target.id)
    assert (source.status, target.week_type, ids["pairs"][0].week_id) == ("scheduled", "rain_date", source.id)
    ids["pairs"][0].sort_order = 9
    session.flush()
    with pytest.raises(ScheduleConflict, match="changed after this preview"):
        confirm_rain_date(session, ids["season"], source.id, target.id,
                           expected_fingerprint=preview.fingerprint)
    assert (source.status, target.week_type, ids["pairs"][0].week_id) == ("scheduled", "rain_date", source.id)


@pytest.mark.parametrize("case", ["played_source", "played_target", "conflicting_reservation", "occupied_target"])
def test_rain_date_activation_conflicts_are_refused_without_changes(session, case):
    ids = _create_case(session, pairings=0, snapshot_count=0)
    source, target = ids["weeks"][8], ids["weeks"][9]
    if case == "played_source":
        source.status = "played"
    elif case == "played_target":
        target.status = "played"
    elif case == "conflicting_reservation":
        target.makeup_for_week_id = ids["weeks"][7].id
    else:
        target_match = TeamMatch(week_id=target.id, home_team_id=1, away_team_id=2,
                                 is_self_match=False, sort_order=1)
        session.add(target_match)
        session.flush()
    session.flush()
    with pytest.raises((ScheduleConflict, ScheduleValidationError)):
        preview_rain_date(session, ids["season"], source.id, target.id)
    assert source.status != "cancelled" and target.week_type == "rain_date"


def test_wrong_season_self_target_and_played_week_one_are_rejected(session):
    ids = _create_case(session, pairings=0, snapshot_count=0)
    source, target = ids["weeks"][8], ids["weeks"][9]
    season = session.get(Season, ids["season"])
    foreign_season = Season(year=2027, course_id=season.course_id,
                            start_date=date(2027, 8, 26), end_date=date(2027, 9, 2),
                            play_weekday=3, status="draft", first_week_nine="front")
    session.add(foreign_season)
    session.flush()
    foreign_target = Week(season_id=foreign_season.id, index=2, play_date=date(2027, 9, 2),
                          nine=None, week_type="rain_date", status="scheduled")
    session.add(foreign_target)
    session.flush()
    with pytest.raises(LookupError):
        preview_rain_date(session, ids["season"], source.id, foreign_target.id)
    with pytest.raises(ScheduleValidationError):
        preview_rain_date(session, ids["season"], source.id, source.id)
    source.makeup_for_week_id = target.id
    session.flush()
    with pytest.raises(ScheduleValidationError, match="cycle"):
        preview_rain_date(session, ids["season"], source.id, target.id)
    source.makeup_for_week_id = None
    first = ids["weeks"][0]
    first.week_type, first.status = "match", "played"
    with pytest.raises(ScheduleConflict):
        preview_rain_date(session, ids["season"], first.id, target.id)


def test_activation_rolls_back_all_rows_on_injected_database_failure(tmp_path):
    rollback_engine = make_engine(f"sqlite:///{tmp_path / 'rain-date-rollback.db'}")
    Base.metadata.create_all(rollback_engine)
    with Session(rollback_engine) as setup:
        ids = _create_case(setup, pairings=1, snapshot_count=1)
        setup.commit()
        season_id = ids["season"]
        source_id, target_id = ids["weeks"][8].id, ids["weeks"][9].id
        pair_id, snapshot_id = ids["pairs"][0].id, ids["snapshots"][0].id
    with Session(rollback_engine) as session:
        preview = preview_rain_date(session, season_id, source_id, target_id)

    def fail_snapshot_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE week_handicaps"):
            raise RuntimeError("Synthetic snapshot move fault")

    event.listen(rollback_engine, "before_cursor_execute", fail_snapshot_update)
    try:
        with pytest.raises(RuntimeError, match="snapshot move fault"):
            confirm_rain_date(session, season_id, source_id, target_id,
                              expected_fingerprint=preview.fingerprint)
    finally:
        event.remove(rollback_engine, "before_cursor_execute", fail_snapshot_update)
    with Session(rollback_engine) as verify:
        assert verify.get(Week, source_id).status == "scheduled"
        assert verify.get(Week, target_id).week_type == "rain_date"
        assert verify.scalar(select(TeamMatch.week_id).where(TeamMatch.id == pair_id)) == source_id
        assert verify.scalar(select(WeekHandicap.week_id).where(WeekHandicap.id == snapshot_id)) == source_id
    rollback_engine.dispose()


def test_two_concurrent_confirms_are_serialized_and_idempotent(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'rain-date-race.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ids = _create_case(session, pairings=1, snapshot_count=1)
        session.commit()
        season_id = ids["season"]
        source_id, target_id = ids["weeks"][8].id, ids["weeks"][9].id
        pair_id = ids["pairs"][0].id
    factory = sessionmaker(engine)
    barrier = Barrier(2)

    def confirm_once():
        with factory() as session:
            preview = preview_rain_date(session, season_id, source_id, target_id)
            session.commit()
            barrier.wait(timeout=5)
            result = confirm_rain_date(session, season_id, source_id, target_id,
                                       expected_fingerprint=preview.fingerprint)
            return result.match_count

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: confirm_once(), range(2))) == [1, 1]
        with Session(engine) as session:
            assert session.scalar(select(TeamMatch.week_id).where(TeamMatch.id == pair_id)) == target_id
    finally:
        engine.dispose()


def test_admin_preview_cancel_and_confirm_contract(admin_client):
    from golf_league.models import Week

    session = Session(bind=admin_client.app.state.engine)
    try:
        ids = _create_case(session, pairings=0, snapshot_count=0)
        session.commit()
        season_id = ids["season"]
        source_id, target_id = ids["weeks"][8].id, ids["weeks"][9].id
    finally:
        session.close()
    page = admin_client.get(f"/admin/seasons/{season_id}/weeks")
    csrf = _extract_csrf(page.text)
    preview_response = admin_client.post(
        f"/admin/seasons/{season_id}/weeks/{source_id}/rain-date/preview",
        data={"csrf_token": csrf, "target_week_id": str(target_id)}, follow_redirects=False,
    )
    assert preview_response.status_code == 200
    assert "Use rain date" in preview_response.text and "does not shift dates" in preview_response.text
    check = Session(bind=admin_client.app.state.engine)
    try:
        assert check.get(Week, source_id).status == "scheduled"
        assert check.get(Week, target_id).week_type == "rain_date"
    finally:
        check.close()
    csrf = _extract_csrf(preview_response.text)
    import re
    fingerprint = re.search(r'name="fingerprint" value="([^"]+)', preview_response.text).group(1)
    signature = re.search(r'name="signature" value="([^"]+)', preview_response.text).group(1)
    confirm_response = admin_client.post(
        f"/admin/seasons/{season_id}/weeks/{source_id}/rain-date/confirm",
        data={"csrf_token": csrf, "target_week_id": str(target_id),
              "fingerprint": fingerprint, "signature": signature}, follow_redirects=False,
    )
    assert confirm_response.status_code == 303
    check = Session(bind=admin_client.app.state.engine)
    try:
        assert check.get(Week, source_id).status == "cancelled"
        assert check.get(Week, target_id).week_type == "match"
    finally:
        check.close()
