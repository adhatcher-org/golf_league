"""Historical seed snapshots, atomic generation and migration acceptance."""

from datetime import date

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from test_matchup_generation import NOW, clock, create_generated, seed_fixture
from test_matchup_generation import matchup_db as _matchup_db

from golf_league.database import make_engine
from golf_league.migrations import _make_config as make_config
from golf_league.models import (
    PlayerMatch,
    Season,
    SeasonParticipant,
    TeeSet,
    Week,
    WeekHandicap,
)
from golf_league.services import handicaps
from golf_league.services.courses import CourseValidationError, delete_tee_set
from golf_league.services.matchups import (
    MatchupConflict,
    generate_player_matches,
    generate_week_matches,
)
from golf_league.services.roster import RosterValidationError, delete_golfer
from golf_league.services.schedule import (
    ScheduleConflict,
    confirm_nine,
    delete_week,
    preview_nine,
)

matchup_db = _matchup_db


def snapshots(session, week_id=None):
    query = select(WeekHandicap).order_by(WeekHandicap.id)
    if week_id is not None:
        query = query.where(WeekHandicap.week_id == week_id)
    return list(session.scalars(query))


def stored(rows):
    return [(r.id, r.week_id, r.golfer_id, r.tee_set_id, r.nine, r.strokes, r.source, r.computed_at) for r in rows]


def generated(session, ids, week_index=0):
    return generate_week_matches(session, ids["season"], week_id=ids["weeks"][week_index],
                                 home_team_id=ids["teams"][0], away_team_id=ids["teams"][1], clock=clock)


def test_current_effective_seed_first_creation_order_and_historical_reuse(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        pair, rows, _ = generated(session, ids, 2)
        initial = stored(snapshots(session, ids["weeks"][2]))
        assert len(initial) == 8 and all(r[6] == "seeded" and r[7] == NOW for r in initial)
        assert [r[5] for r in initial] == [-1, -1, 0, 0, 1, 1, 2, 2]
        session.add(SeasonParticipant(season_id=ids["season"], golfer_id=ids["golfers"][0], seed_handicap_strokes=-4))
        session.commit()
        generated(session, ids, 0)
        later = snapshots(session, ids["weeks"][0])
        assert all(r.source == "carried" for r in later)
        assert next(r.strokes for r in later if r.golfer_id == ids["golfers"][0]) == -4
        assert stored(snapshots(session, ids["weeks"][2])) == initial
        before_ids = [row.id for row in rows]
        repeated = generate_player_matches(session, pair.id, clock=clock)
        assert [row.id for row in repeated] == before_ids
        assert stored(snapshots(session, ids["weeks"][2])) == initial


def test_conflicting_existing_tee_and_nine_refuse_regeneration(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        pair, _, _ = generated(session, ids)
        original = stored(snapshots(session))
        season = session.get(Season, ids["season"])
        gold = session.scalar(select(TeeSet).where(TeeSet.course_id == season.course_id, TeeSet.name == "Snake"))
        override = SeasonParticipant(season_id=season.id, golfer_id=ids["golfers"][0], tee_set_id=gold.id, seed_handicap_strokes=-2)
        session.add(override)
        session.commit()
        with pytest.raises(MatchupConflict, match="conflicting"):
            generate_week_matches(session, ids["season"], team_match_id=pair.id, clock=clock)
        assert stored(snapshots(session)) == original
        override.tee_set_id = ids["tee"]
        session.get(Week, ids["weeks"][0]).nine = "back"
        session.commit()
        with pytest.raises(MatchupConflict):
            generate_week_matches(session, ids["season"], team_match_id=pair.id, clock=clock)
        assert stored(snapshots(session)) == original


def test_snapshot_fault_rolls_back_new_pairing_and_existing_generation(matchup_db, monkeypatch):
    engine, ids = matchup_db
    original = handicaps.ensure_week_handicap
    calls = 0
    def fault(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("Synthetic snapshot write fault")
        return original(*args, **kwargs)
    monkeypatch.setattr(handicaps, "ensure_week_handicap", fault)
    with Session(engine) as session:
        with pytest.raises(RuntimeError):
            generated(session, ids)
        assert list(session.scalars(select(PlayerMatch))) == [] and snapshots(session) == []
        from golf_league.models import TeamMatch
        assert list(session.scalars(select(TeamMatch))) == []
    monkeypatch.setattr(handicaps, "ensure_week_handicap", original)
    with Session(engine) as session:
        pairing, rows, _ = generated(session, ids)
        before = stored(snapshots(session))
        ids_before = [row.id for row in rows]
        monkeypatch.setattr(handicaps, "ensure_week_handicap", fault)
        calls = 0
        with pytest.raises(RuntimeError):
            generate_week_matches(session, ids["season"], team_match_id=pairing.id, clock=clock)
        assert stored(snapshots(session)) == before
        assert [r.id for r in session.scalars(select(PlayerMatch).order_by(PlayerMatch.id))] == ids_before


def test_nine_relabel_atomic_and_historical_metadata_preserved(matchup_db, monkeypatch):
    engine, ids = matchup_db
    with Session(engine) as session:
        generated(session, ids)
        before = stored(snapshots(session))
        preview = preview_nine(session, ids["season"], ids["weeks"][0], new_nine="back")
        original = handicaps.relabel_week_nine
        def fault(*args):
            original(*args)
            raise RuntimeError("Synthetic relabel fault")
        monkeypatch.setattr(handicaps, "relabel_week_nine", fault)
        with pytest.raises(RuntimeError):
            confirm_nine(session, ids["season"], ids["weeks"][0], new_nine="back", expected_fingerprint=preview.fingerprint)
        assert stored(snapshots(session)) == before
        assert session.get(Week, ids["weeks"][0]).nine == "front"
        monkeypatch.setattr(handicaps, "relabel_week_nine", original)
        confirm_nine(session, ids["season"], ids["weeks"][0], new_nine="back", expected_fingerprint=preview.fingerprint)
        after = stored(snapshots(session))
        assert all(row[4] == "back" for row in after)
        assert [(*row[:4], *row[5:]) for row in after] == [(*row[:4], *row[5:]) for row in before]


def test_snapshot_only_references_guard_week_golfer_and_tee_deletion(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        handicaps.ensure_week_handicap(session, ids["weeks"][0], ids["golfers"][0], clock=clock)
        session.commit()
        with pytest.raises(ScheduleConflict, match="1 week handicap"):
            delete_week(session, ids["season"], ids["weeks"][0])
        with pytest.raises(RosterValidationError, match="1 week handicap"):
            delete_golfer(session, ids["golfers"][0])
        season = session.get(Season, ids["season"])
        with pytest.raises(CourseValidationError, match="1 week handicaps"):
            delete_tee_set(session, season.course_id, ids["tee"])


@pytest.mark.parametrize("prior", [False, True])
def test_snapshot_migration_fresh_and_prior_data_preserved(tmp_path, prior):
    url = f"sqlite:///{tmp_path / 'snapshot-migration.db'}"
    config = make_config(url)
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
    ids = None
    player_ids = None
    if prior:
        command.upgrade(config, "9d5b4a7c2e61")
        engine = make_engine(url)
        with Session(engine) as session:
            ids = seed_fixture(session)
            _, rows, _ = create_generated(session, ids)
            player_ids = [row.id for row in rows]
            session.commit()
        engine.dispose()
    command.upgrade(config, "head")
    engine = make_engine(url)
    try:
        schema = inspect(engine)
        assert len(schema.get_foreign_keys("week_handicaps")) == 3
        assert schema.get_unique_constraints("week_handicaps")[0]["column_names"] == ["week_id", "golfer_id"]
        with Session(engine) as session:
            if ids is None:
                ids = seed_fixture(session)
            assert session.get(Week, ids["weeks"][0]).play_date == date(2026, 8, 27)
            if prior:
                assert [r.id for r in session.scalars(select(PlayerMatch).order_by(PlayerMatch.id))] == player_ids
            assert snapshots(session) == []
    finally:
        engine.dispose()
    command.downgrade(config, "9d5b4a7c2e61")
    engine = make_engine(url)
    try:
        assert "week_handicaps" not in inspect(engine).get_table_names()
        assert "player_matches" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
