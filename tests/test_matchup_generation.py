"""Synthetic GL-35 schema, eligibility, stable generation and transaction acceptance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta, timezone
from threading import Barrier

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.database import make_engine
from golf_league.domain.matchups import pairing_shape, validate_team_once_per_week
from golf_league.migrations import _make_config as make_config
from golf_league.migrations import current_revision
from golf_league.models import (
    Base,
    Course,
    Golfer,
    PlayerMatch,
    Season,
    SeasonGolfer,
    SeasonParticipant,
    Team,
    TeamMatch,
    TeamMember,
    TeeSet,
    UTCDateTime,
    Week,
)
from golf_league.services.course_seed import seed_wyandot
from golf_league.services.matchups import (
    MatchupConflict,
    MatchupValidationError,
    create_team_match,
)
from golf_league.services.matchups import (
    _generate_player_matches as generate_player_matches,
)
from golf_league.services.roster import RosterValidationError, delete_golfer
from golf_league.services.schedule import ScheduleConflict, delete_week

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def clock():
    return NOW


def seed_fixture(session):
    seed_wyandot(session)
    course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
    tee = session.scalar(select(TeeSet).where(TeeSet.course_id == course.id, TeeSet.name == "Deer"))
    season = Season(year=2026, course_id=course.id, start_date=date(2026, 8, 27), end_date=date(2026, 10, 29),
                    play_weekday=3, status="draft", first_week_nine="front")
    session.add(season)
    session.flush()
    teams, golfers = [], []
    for number, label in enumerate("ABCD", 1):
        team = Team(season_id=season.id, name=label, number=number, sort_order=number)
        session.add(team)
        session.flush()
        teams.append(team.id)
        for position in range(1, 5):
            golfer = Golfer(first_name=f"{label}{position}", last_name="Example", default_tee_set_id=tee.id,
                            handicap_strokes=position - 2, handicap_source="self_reported", handicap_status="ok")
            session.add(golfer)
            session.flush()
            golfers.append(golfer.id)
            session.add(SeasonGolfer(season_id=season.id, golfer_id=golfer.id))
            session.add(TeamMember(team_id=team.id, season_id=season.id, golfer_id=golfer.id, position=position))
    weeks = []
    for index in range(1, 4):
        week = Week(season_id=season.id, index=index, play_date=date(2026, 8, 27) + timedelta(days=7 * (index - 1)),
                    nine="front" if index % 2 else "back", week_type="match", status="scheduled")
        session.add(week)
        session.flush()
        weeks.append(week.id)
    session.commit()
    return {"season": season.id, "teams": teams, "golfers": golfers, "weeks": weeks, "tee": tee.id}


@pytest.fixture
def matchup_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'matchups.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ids = seed_fixture(session)
    yield engine, ids
    engine.dispose()


def create_generated(session, ids, *, self_match=False, week_index=0):
    pairing, warnings = create_team_match(session, ids["weeks"][week_index], ids["teams"][0],
                                          ids["teams"][0 if self_match else 1], clock=clock)
    rows = generate_player_matches(session, pairing.id, clock=clock)
    return pairing, rows, warnings


def contents(rows):
    return [(r.id, r.position_label, r.a_golfer_id, r.b_golfer_id, r.a_is_sub, r.b_is_sub,
             r.vs_own_handicap, r.manually_adjusted, r.generated_at) for r in rows]


def counts(engine):
    with Session(engine) as session:
        return (session.scalar(select(func.count(TeamMatch.id))), session.scalar(select(func.count(PlayerMatch.id))))


def test_pure_shapes_and_cross_side_team_once():
    assert pairing_shape(1, 2) == [(str(p), p, p, False) for p in range(1, 5)]
    assert pairing_shape(1, 1) == [("P1vP2", 1, 2, True), ("P3vP4", 3, 4, True)]
    validate_team_once_per_week(1, 1, [(2, 3)])
    for home, away, existing in [(1, 2, [(3, 1)]), (3, 1, [(1, 2)]), (1, 1, [(3, 1)])]:
        with pytest.raises(ValueError):
            validate_team_once_per_week(home, away, existing)


@pytest.mark.parametrize("self_match", [False, True])
def test_correct_shapes_repeat_ids_content_and_aware_utc_reload(matchup_db, self_match):
    engine, ids = matchup_db
    with Session(engine) as session:
        pairing, rows, warnings = create_generated(session, ids, self_match=self_match)
        assert warnings == [] and pairing.is_self_match == self_match
        expected = [("P1vP2", ids["golfers"][0], ids["golfers"][1]), ("P3vP4", ids["golfers"][2], ids["golfers"][3])] if self_match else [(str(p), ids["golfers"][p - 1], ids["golfers"][p + 3]) for p in range(1, 5)]
        assert [(r.position_label, r.a_golfer_id, r.b_golfer_id) for r in rows] == expected
        assert all(r.vs_own_handicap == self_match for r in rows)
        before = contents(rows)
        pairing_id = pairing.id
        session.commit()
    with Session(engine) as session:
        repeated = generate_player_matches(session, pairing_id, clock=lambda: NOW + timedelta(days=10))
        assert contents(repeated) == before
        assert all(r.generated_at.tzinfo is UTC for r in repeated)
        session.commit()
    assert counts(engine) == (1, 2 if self_match else 4)


def test_manual_substitution_and_new_member_position_survive(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        pairing, rows, _ = create_generated(session, ids)
        substitute = Golfer(first_name="Sub", last_name="Example", default_tee_set_id=ids["tee"],
                            handicap_strokes=0, handicap_source="self_reported", handicap_status="ok")
        session.add(substitute)
        session.flush()
        rows[0].a_golfer_id = substitute.id
        rows[0].a_is_sub = True
        rows[0].manually_adjusted = True
        session.commit()
        before = contents(rows)[0]
        members = list(session.scalars(select(TeamMember).where(TeamMember.team_id == ids["teams"][0], TeamMember.position.in_([1, 2]))))
        for member in members:
            session.delete(member)
        session.flush()
        session.add_all([TeamMember(team_id=ids["teams"][0], season_id=ids["season"], golfer_id=ids["golfers"][1], position=1),
                         TeamMember(team_id=ids["teams"][0], season_id=ids["season"], golfer_id=ids["golfers"][0], position=2)])
        session.commit()
        repeated = generate_player_matches(session, pairing.id, clock=lambda: NOW + timedelta(days=1))
        assert contents(repeated)[0] == before
        assert repeated[1].a_golfer_id == ids["golfers"][0]
        assert repeated[1].generated_at == NOW + timedelta(days=1)
        session.commit()
    assert counts(engine) == (1, 4)


@pytest.mark.parametrize("self_match", [False, True])
def test_duplicate_team_refused_but_reversed_pair_other_week_warns(matchup_db, self_match):
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids, self_match=self_match)
        session.commit()
        with pytest.raises(MatchupValidationError):
            create_team_match(session, ids["weeks"][0], ids["teams"][2], ids["teams"][0], clock=clock)
        session.rollback()
        pairing, warnings = create_team_match(session, ids["weeks"][1], ids["teams"][0 if self_match else 1], ids["teams"][0], clock=clock)
        assert len(warnings) == 1 and "1" in warnings[0]
        generate_player_matches(session, pairing.id, clock=clock)
        session.commit()
    assert counts(engine) == (2, 4 if self_match else 8)


@pytest.mark.parametrize("fault", ["seed", "override_no_seed", "missing_tee", "foreign_override", "inactive", "not_roster", "incomplete", "unrelated_incomplete"])
def test_invalid_member_or_effective_eligibility_refused_without_writes(matchup_db, fault):
    engine, ids = matchup_db
    with Session(engine) as session:
        golfer = session.get(Golfer, ids["golfers"][0])
        if fault == "seed":
            golfer.handicap_strokes = None
        elif fault == "override_no_seed":
            session.add(SeasonParticipant(season_id=ids["season"], golfer_id=golfer.id, tee_set_id=ids["tee"]))
        elif fault == "missing_tee":
            golfer.default_tee_set_id = None
            golfer.default_tee_label = "Unavailable"
        elif fault == "foreign_override":
            course = Course(name="Other Club", total_holes=18)
            session.add(course)
            session.flush()
            tee = TeeSet(course_id=course.id, name="Other", color_label="White", gender="men", total_yards=6000, sort_order=1)
            session.add(tee)
            session.flush()
            session.add(SeasonParticipant(season_id=ids["season"], golfer_id=golfer.id, tee_set_id=tee.id, seed_handicap_strokes=0))
        elif fault == "inactive":
            golfer.is_active = False
        elif fault == "not_roster":
            session.delete(session.scalar(select(SeasonGolfer).where(SeasonGolfer.golfer_id == golfer.id)))
        else:
            team = ids["teams"][2 if fault == "unrelated_incomplete" else 0]
            session.delete(session.scalar(select(TeamMember).where(TeamMember.team_id == team, TeamMember.position == 4)))
        session.commit()
        with pytest.raises(MatchupValidationError):
            create_generated(session, ids)
        session.rollback()
    assert counts(engine) == (0, 0)


def test_nonnull_override_seed_and_label_fallback_beat_missing_roster_seed(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        golfer = session.get(Golfer, ids["golfers"][0])
        golfer.handicap_strokes = None
        golfer.default_tee_set_id = None
        golfer.default_tee_label = "White"
        session.add(SeasonParticipant(season_id=ids["season"], golfer_id=golfer.id, tee_set_id=ids["tee"], seed_handicap_strokes=-2))
        session.commit()
        _, rows, _ = create_generated(session, ids)
        assert len(rows) == 4
        session.commit()


@pytest.mark.parametrize("status,kind,nine", [("cancelled", "match", "front"), ("played", "match", "front"),
                                            ("scheduled", "play_with_team", "front"), ("scheduled", "rain_date", None)])
def test_week_kind_and_status_refusals(matchup_db, status, kind, nine):
    engine, ids = matchup_db
    with Session(engine) as session:
        week = session.get(Week, ids["weeks"][0])
        week.status, week.week_type, week.nine = status, kind, nine
        session.commit()
        with pytest.raises(MatchupValidationError):
            create_generated(session, ids)
        session.rollback()
    assert counts(engine) == (0, 0)


def test_missing_and_cross_season_resources(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        original = session.get(Season, ids["season"])
        season = Season(year=2027, course_id=original.course_id, start_date=date(2027, 8, 26), end_date=date(2027, 9, 2), play_weekday=3, status="draft", first_week_nine="front")
        session.add(season)
        session.flush()
        foreign = Team(season_id=season.id, name="Foreign", number=1, sort_order=1)
        session.add(foreign)
        session.commit()
        for week, home, away in [(99999, *ids["teams"][:2]), (ids["weeks"][0], 99999, ids["teams"][0]), (ids["weeks"][0], foreign.id, ids["teams"][0])]:
            with pytest.raises(LookupError):
                create_team_match(session, week, home, away, clock=clock)
            session.rollback()
        with pytest.raises(LookupError):
            generate_player_matches(session, 99999, clock=clock)
        session.rollback()


def test_protected_slot_shape_conflict_has_no_destructive_writes(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        pairing, rows, _ = create_generated(session, ids)
        rows[0].manually_adjusted = True
        session.commit()
        before = contents(rows)
        pairing.away_team_id, pairing.is_self_match = pairing.home_team_id, True
        session.commit()
        with pytest.raises(MatchupConflict, match="Protected slots: 1"):
            generate_player_matches(session, pairing.id, clock=clock)
        session.rollback()
        assert contents(list(session.scalars(select(PlayerMatch).order_by(PlayerMatch.id)))) == before


def test_protected_participant_checked_instead_of_replaced_member_seed(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        pairing, rows, _ = create_generated(session, ids)
        sub = Golfer(first_name="Alternate", last_name="Example", default_tee_set_id=ids["tee"], handicap_strokes=-1, handicap_source="self_reported", handicap_status="ok")
        session.add(sub)
        session.flush()
        rows[0].a_golfer_id, rows[0].a_is_sub, rows[0].manually_adjusted = sub.id, True, True
        session.get(Golfer, ids["golfers"][0]).handicap_strokes = None
        session.commit()
        before = contents(rows)
        assert contents(generate_player_matches(session, pairing.id, clock=clock)) == before
        session.commit()
        sub.handicap_strokes = None
        session.commit()
        assert contents(generate_player_matches(session, pairing.id, clock=clock)) == before
        session.rollback()
        assert contents(list(session.scalars(select(PlayerMatch).order_by(PlayerMatch.id)))) == before


def test_caller_transaction_rolls_back_forced_downstream_and_partial_flush_faults(matchup_db):
    engine, ids = matchup_db
    with pytest.raises(RuntimeError, match="Downstream"):
        with Session(engine) as session, session.begin():
            create_generated(session, ids)
            raise RuntimeError("Downstream snapshot placeholder fault")
    assert counts(engine) == (0, 0)
    inserts = 0
    def fail_second_insert(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserts
        if statement.startswith("INSERT INTO player_matches"):
            inserts += 1
            if inserts == 2:
                raise RuntimeError("Forced player insert fault")
    event.listen(engine, "before_cursor_execute", fail_second_insert)
    try:
        with pytest.raises(RuntimeError, match="insert fault"):
            with Session(engine) as session, session.begin():
                create_generated(session, ids)
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_insert)
    assert counts(engine) == (0, 0)


@pytest.mark.parametrize("self_match", [False, True])
def test_opposite_side_concurrent_pairings_on_independent_connections(matchup_db, self_match):
    engine, ids = matchup_db
    barrier = Barrier(2)
    candidates = [(ids["teams"][0], ids["teams"][0 if self_match else 1]), (ids["teams"][2], ids["teams"][0])]
    def attempt(pair):
        with Session(engine) as session:
            session.get(Week, ids["weeks"][0])  # An existing SQLAlchemy read transaction.
            barrier.wait(timeout=10)
            try:
                pairing, _ = create_team_match(session, ids["weeks"][0], *pair, clock=clock)
                generated = generate_player_matches(session, pairing.id, clock=clock)
                session.commit()
                return len(generated)
            except (MatchupValidationError, MatchupConflict):
                session.rollback()
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, candidates))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert counts(engine) == (1, winners[0])


def test_concurrent_generation_keeps_one_row_per_slot(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        pairing, _ = create_team_match(session, ids["weeks"][0], *ids["teams"][:2], clock=clock)
        pairing_id = pairing.id
        session.commit()
    barrier = Barrier(2)
    def attempt(_):
        with Session(engine) as session:
            session.get(TeamMatch, pairing_id)
            barrier.wait(timeout=10)
            rows = generate_player_matches(session, pairing_id, clock=clock)
            result = contents(rows)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results[0] == results[1]
    assert counts(engine) == (1, 4)


def test_cached_eligibility_refreshed_and_busy_writer_is_retriable(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as cached, Session(engine) as writer:
        cached.get(Golfer, ids["golfers"][0])
        cached.get(Week, ids["weeks"][0])
        writer.get(Golfer, ids["golfers"][0]).handicap_strokes = None
        writer.commit()
        with pytest.raises(MatchupValidationError):
            create_generated(cached, ids)
        cached.rollback()
        cached.connection().exec_driver_sql("PRAGMA busy_timeout=1")
        writer.connection().exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(MatchupConflict, match="retry"):
            create_generated(cached, ids)
        cached.rollback()
        writer.rollback()
    assert counts(engine) == (0, 0)


def test_restrictive_week_golfer_and_team_deletion_and_no_blanket_golfer_uniqueness(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids)
        session.commit()
        with pytest.raises(ScheduleConflict, match="1 team match"):
            delete_week(session, ids["season"], ids["weeks"][0])
        with pytest.raises(RosterValidationError, match="1 player match"):
            delete_golfer(session, ids["golfers"][0])
        with pytest.raises(RosterValidationError, match="1 player match"):
            delete_golfer(session, ids["golfers"][4])
        with pytest.raises(IntegrityError):
            session.connection().exec_driver_sql("DELETE FROM teams WHERE id = ?", (ids["teams"][0],))
        session.rollback()
        # Repeated golfers in distinct slots are legal at the schema boundary.
        first = session.scalar(select(PlayerMatch).where(PlayerMatch.position_label == "1"))
        second = session.scalar(select(PlayerMatch).where(PlayerMatch.position_label == "2"))
        second.a_golfer_id = first.a_golfer_id
        second.b_golfer_id = first.b_golfer_id
        session.commit()
    assert counts(engine) == (1, 4)


def test_generation_clock_validation_and_timestamp_type(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        pairing, _ = create_team_match(session, ids["weeks"][0], *ids["teams"][:2], clock=clock)
        for invalid in (None, datetime(2026, 10, 5), datetime(2026, 10, 5, tzinfo=timezone(timedelta(hours=1)))):
            with pytest.raises(MatchupValidationError):
                generate_player_matches(session, pairing.id, clock=lambda invalid=invalid: invalid)
        session.rollback()
    utc_type = UTCDateTime()
    assert utc_type.process_bind_param(None, None) is None
    assert utc_type.process_result_value(None, None) is None
    assert utc_type.process_result_value(NOW, None) == NOW
    with pytest.raises(ValueError):
        utc_type.process_bind_param(datetime(2026, 10, 5), None)
    assert counts(engine) == (0, 0)


@pytest.mark.parametrize("prior", [False, True])
def test_fresh_and_prior_head_migration_preserves_data_and_one_head(tmp_path, prior):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = make_config(url)
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    engine = None
    ids = None
    if prior:
        command.upgrade(config, "ef71a3b9c052")
        engine = make_engine(url)
        with Session(engine) as session:
            ids = seed_fixture(session)
        engine.dispose()
    command.upgrade(config, "head")
    assert current_revision(url) == heads[0]
    command.upgrade(config, "head")
    engine = make_engine(url)
    try:
        schema = inspect(engine)
        assert {"team_matches", "player_matches"} <= set(schema.get_table_names())
        assert len(schema.get_foreign_keys("team_matches")) == 3
        assert len(schema.get_foreign_keys("player_matches")) == 3
        assert all(fk["options"].get("ondelete") != "CASCADE" for table in ("team_matches", "player_matches") for fk in schema.get_foreign_keys(table))
        assert {tuple(row["column_names"]) for row in schema.get_unique_constraints("player_matches")} == {("team_match_id", "position_label")}
        with Session(engine) as session:
            if ids is None:
                ids = seed_fixture(session)
            assert session.get(Season, ids["season"]).start_date == date(2026, 8, 27)
            assert session.get(Team, ids["teams"][0]).name == "A"
            assert session.get(Week, ids["weeks"][0]).play_date == date(2026, 8, 27)
            create_generated(session, ids)
            session.commit()
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    command.downgrade(config, "ef71a3b9c052")
    engine = make_engine(url)
    try:
        assert "player_matches" not in inspect(engine).get_table_names()
        assert "team_matches" not in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.get(Team, ids["teams"][0]).name == "A"
    finally:
        engine.dispose()




def test_existing_delete_routes_return_409_and_match_reference_counts(admin_client):
    import re

    with Session(admin_client.app.state.engine) as session:
        ids = seed_fixture(session)
        create_generated(session, ids)
        session.commit()
    season_id, week_id = ids["season"], ids["weeks"][0]
    page = admin_client.get(f"/admin/seasons/{season_id}/weeks")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    week_delete = admin_client.post(f"/admin/seasons/{season_id}/weeks/{week_id}/delete", data={"csrf_token": csrf})
    assert week_delete.status_code == 409 and "1 team match" in week_delete.text
    for golfer_id in (ids["golfers"][0], ids["golfers"][4]):
        response = admin_client.post(f"/admin/golfers/{golfer_id}/delete", data={"csrf_token": csrf})
        assert response.status_code == 409 and "1 player match" in response.text
    assert counts(admin_client.app.state.engine) == (1, 4)
