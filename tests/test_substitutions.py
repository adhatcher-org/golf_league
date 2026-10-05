from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import clock, seed_fixture
from test_matchup_generation import matchup_db as _matchup_db

from golf_league.models import (
    Golfer,
    PlayerMatch,
    SeasonGolfer,
    WeekHandicap,
)
from golf_league.services.matchups import (
    MatchupConflict,
    MatchupValidationError,
    generate_week_matches,
    substitute_player,
    substitute_player_core,
)

matchup_db = _matchup_db

def setup_sub(session, ids):
    pairing, rows, _ = generate_week_matches(session, ids['season'], week_id=ids['weeks'][0],
        home_team_id=ids['teams'][0], away_team_id=ids['teams'][1], clock=clock)
    sub = Golfer(first_name='Sub', last_name='Synthetic', default_tee_set_id=ids['tee'],
                 handicap_strokes=-3, handicap_source='self_reported', handicap_status='ok')
    session.add(sub)
    session.commit()
    return ids, pairing.id, [row.id for row in rows], sub.id


def test_substitution_history_and_protected_retry(matchup_db):
    with Session(matchup_db[0]) as session:
        ids, pairing, rows, sub = setup_sub(session, matchup_db[1])
        result = substitute_player(session, ids['season'], rows[0], side='a', golfer_id=sub, clock=clock)
        assert result.a_golfer_id == sub and result.a_is_sub and result.manually_adjusted
        snap = session.scalar(select(WeekHandicap).where(WeekHandicap.golfer_id == sub))
        original = (snap.id, snap.strokes, snap.source, snap.computed_at, snap.tee_set_id)
        golfer = session.get(Golfer, sub)
        golfer.is_active = False
        golfer.handicap_strokes = None
        session.commit()
        assert substitute_player(session, ids['season'], rows[0], side='a', golfer_id=sub, clock=clock).id == rows[0]
        _, regenerated, _ = generate_week_matches(session, ids['season'], team_match_id=pairing, clock=clock)
        assert [r.id for r in regenerated] == rows
        snap = session.get(WeekHandicap, original[0])
        assert (snap.id, snap.strokes, snap.source, snap.computed_at, snap.tee_set_id) == original
        assert session.get(PlayerMatch, rows[0]).a_golfer_id == sub


@pytest.mark.parametrize('side', ['a', 'b'])
def test_once_per_week_both_sides_and_other_week_allowed(matchup_db, side):
    with Session(matchup_db[0]) as session:
        ids, _, rows, sub = setup_sub(session, matchup_db[1])
        substitute_player(session, ids['season'], rows[0], side='a', golfer_id=sub, clock=clock)
        with pytest.raises(MatchupConflict):
            substitute_player(session, ids['season'], rows[1], side=side, golfer_id=sub, clock=clock)
        _, other, _ = generate_week_matches(session, ids['season'], week_id=ids['weeks'][1],
            home_team_id=ids['teams'][0], away_team_id=ids['teams'][1], clock=clock)
        substitute_player(session, ids['season'], other[0].id, side=side, golfer_id=sub, clock=clock)
        assert len(list(session.scalars(select(WeekHandicap).where(WeekHandicap.golfer_id == sub)))) == 2


@pytest.mark.parametrize('invalid', ['inactive', 'missing_seed', 'team', 'side', 'missing', 'foreign'])
def test_substitution_validation_is_atomic(matchup_db, invalid):
    with Session(matchup_db[0]) as session:
        ids, _, rows, sub = setup_sub(session, matchup_db[1])
        golfer = session.get(Golfer, sub)
        if invalid == 'inactive':
            golfer.is_active = False
        if invalid == 'missing_seed':
            golfer.handicap_strokes = None
        if invalid == 'team':
            sub = ids['golfers'][8]
        session.commit()
        error = LookupError if invalid in {'missing', 'foreign'} else MatchupValidationError
        with pytest.raises(error):
            substitute_player(session, ids['season'] + 999 if invalid == 'foreign' else ids['season'],
                rows[0], side='x' if invalid == 'side' else 'a',
                golfer_id=99999 if invalid == 'missing' else sub, clock=clock)
        assert session.get(PlayerMatch, rows[0]).a_golfer_id == ids['golfers'][0]
        assert len(list(session.scalars(select(WeekHandicap)))) == 8


def test_selected_player_without_team_can_sub_and_core_does_not_commit(matchup_db):
    with Session(matchup_db[0]) as session:
        ids, _, rows, sub = setup_sub(session, matchup_db[1])
        session.add(SeasonGolfer(season_id=ids['season'], golfer_id=sub))
        session.commit()
        substitute_player_core(session, ids['season'], rows[0], side='b', golfer_id=sub, clock=clock)
        session.rollback()
        assert session.get(PlayerMatch, rows[0]).b_golfer_id == ids['golfers'][4]
        assert session.scalar(select(WeekHandicap).where(WeekHandicap.golfer_id == sub)) is None


def test_serialized_concurrent_substitution(matchup_db):
    with Session(matchup_db[0]) as session:
        ids, _, rows, sub = setup_sub(session, matchup_db[1])
    barrier = Barrier(2)
    def change(row):
        with Session(matchup_db[0]) as session:
            barrier.wait()
            try:
                substitute_player(session, ids['season'], row, side='b', golfer_id=sub, clock=clock)
                return 'ok'
            except MatchupConflict:
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, rows[:2])) == ['conflict', 'ok']
    with Session(matchup_db[0]) as session:
        assert len(list(session.scalars(select(WeekHandicap).where(WeekHandicap.golfer_id == sub)))) == 1


def test_other_season_team_does_not_exclude_substitute(matchup_db):
    from golf_league.models import TeamMember
    with Session(matchup_db[0]) as session:
        ids, _, rows, sub = setup_sub(session, matchup_db[1])
        other = seed_fixture(session)
        member = session.scalar(select(TeamMember).where(TeamMember.team_id == other['teams'][0], TeamMember.position == 4))
        member.golfer_id = sub
        session.commit()
        substitute_player(session, ids['season'], rows[0], side='b', golfer_id=sub, clock=clock)
        row = session.get(PlayerMatch, rows[0])
        assert row.b_golfer_id == sub and row.b_is_sub and row.id == rows[0]


@pytest.mark.parametrize('kind', ['matching', 'tee', 'nine', 'fault'])
def test_snapshot_reuse_conflict_and_substitution_fault_atomic(matchup_db, monkeypatch, kind):
    from golf_league.models import TeeSet
    from golf_league.services import handicaps
    with Session(matchup_db[0]) as session:
        ids, _, rows, sub = setup_sub(session, matchup_db[1])
        snap = handicaps.ensure_week_handicap(session, ids['weeks'][0], sub, clock=clock)
        session.commit()
        snap_id = snap.id
        original = (snap.id, snap.strokes, snap.source, snap.computed_at, snap.tee_set_id, snap.nine)
        if kind == 'tee':
            golfer = session.get(Golfer, sub)
            golfer.default_tee_set_id = session.scalar(select(TeeSet.id).where(TeeSet.id != ids['tee']))
            golfer.handicap_strokes = -1
            session.commit()
        if kind == 'nine':
            snap.nine = 'back'
            session.commit()
        if kind == 'fault':
            def fail(*args, **kwargs):
                raise RuntimeError('synthetic snapshot fault')
            monkeypatch.setattr(handicaps, 'ensure_week_handicap', fail)
        if kind == 'matching':
            substitute_player(session, ids['season'], rows[0], side='b', golfer_id=sub, clock=clock)
            saved = session.get(WeekHandicap, snap_id)
            assert (saved.id, saved.strokes, saved.source, saved.computed_at, saved.tee_set_id, saved.nine) == original
        else:
            with pytest.raises(RuntimeError if kind == 'fault' else MatchupConflict):
                substitute_player(session, ids['season'], rows[0], side='b', golfer_id=sub, clock=clock)
            assert session.get(PlayerMatch, rows[0]).b_golfer_id == ids['golfers'][4]
            assert session.scalar(select(WeekHandicap.id).where(WeekHandicap.golfer_id == sub)) == snap_id
