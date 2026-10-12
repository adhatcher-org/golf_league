"""GL-79 score storage and the submit service (synthetic data only)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_matchup_generation import create_generated
from test_matchup_generation import matchup_db as _matchup_db

import golf_league.services.scores as scores_module
from golf_league.models import (
    Golfer,
    Hole,
    HoleScore,
    PlayerMatch,
    TeamMatch,
    User,
    Week,
)
from golf_league.services.scores import (
    ScoreEntryError,
    card_candidates,
    default_week,
    eligible_weeks,
    league_today,
    scores_for_week,
    submit_card,
    week_for_entry,
    week_holes,
)

TODAY = date(2026, 8, 27)

matchup_db = _matchup_db


def _user(session):
    user = User(username="admin@example.test", email="admin@example.test", display_name="Admin", password_hash="x")
    session.add(user)
    session.flush()
    return user.id


def _setup(engine, ids, *, extra_weeks=()):
    """Generate week 1 (A vs B) and optional extra week indexes; return user id."""
    with Session(engine) as session:
        for index in (0, *extra_weeks):
            create_generated(session, ids, week_index=index)
        user_id = _user(session)
        session.commit()
    return user_id


def _matches(session, week_id):
    return list(session.scalars(
        select(PlayerMatch).join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id).order_by(PlayerMatch.id)
    ))


def _card(session, week_id, golfer_ids, value=4):
    numbers = [hole.number for hole in week_holes(session, week_id)]
    return [(golfer_id, dict.fromkeys(numbers, value)) for golfer_id in golfer_ids]


def _count(engine):
    with Session(engine) as session:
        return session.scalar(select(func.count(HoleScore.id)))


def test_league_today_is_chicago_evening_before_utc_midnight_rollover():
    instant = datetime(2026, 8, 28, 2, 30, tzinfo=UTC)  # 21:30 on 27 Aug in Chicago
    assert league_today(lambda: instant, "America/Chicago") == date(2026, 8, 27)
    assert league_today(lambda: instant, "UTC") == date(2026, 8, 28)


def test_eligible_and_default_weeks(matchup_db):
    engine, ids = matchup_db
    _setup(engine, ids, extra_weeks=(2,))
    with Session(engine) as session:
        # week 2 has no matches; add an unactivated rain date without matches.
        session.add(Week(season_id=ids["season"], index=4, play_date=date(2026, 9, 17), nine=None,
                         week_type="rain_date", status="scheduled"))
        session.commit()
        assert [w.id for w in eligible_weeks(session, ids["season"], TODAY)] == [ids["weeks"][0], ids["weeks"][2]]
        assert default_week(session, ids["season"], TODAY).id == ids["weeks"][0]
        # No week dated today: next eligible by date.
        assert default_week(session, ids["season"], date(2026, 8, 30)).id == ids["weeks"][2]
        # Past week excluded; cancelled and played excluded.
        session.get(Week, ids["weeks"][0]).status = "cancelled"
        session.get(Week, ids["weeks"][2]).status = "played"
        session.commit()
        assert eligible_weeks(session, ids["season"], TODAY) == []
        assert default_week(session, ids["season"], TODAY) is None


def test_activated_rain_date_with_matches_is_included(matchup_db):
    engine, ids = matchup_db
    _setup(engine, ids, extra_weeks=(1,))
    with Session(engine) as session:
        week = session.get(Week, ids["weeks"][1])
        week.week_type, week.makeup_for_week_id = "match", ids["weeks"][0]
        session.commit()
        assert ids["weeks"][1] in [w.id for w in eligible_weeks(session, ids["season"], date(2026, 9, 1))]


def test_week_for_entry_codes(matchup_db):
    engine, ids = matchup_db
    _setup(engine, ids)
    with Session(engine) as session:
        assert week_for_entry(session, ids["weeks"][0], TODAY).id == ids["weeks"][0]
        def code(week_id, today=TODAY):
            with pytest.raises(ScoreEntryError) as caught:
                week_for_entry(session, week_id, today)
            return caught.value.code
        assert code(999999) == "not_found"
        assert code(ids["weeks"][0], date(2026, 8, 28)) == "past_week"
        assert code(ids["weeks"][1]) == "no_matchup"
        week = session.get(Week, ids["weeks"][0])
        week.status = "cancelled"
        assert code(week.id) == "cancelled"
        week.status = "played"
        assert code(week.id) == "played"
        session.rollback()


def test_candidates_include_substitute_and_exclude_outsiders(matchup_db):
    engine, ids = matchup_db
    _setup(engine, ids)
    outsider = ids["golfers"][8]  # team C member, not in week 1
    sub = ids["golfers"][9]
    with Session(engine) as session:
        match = _matches(session, ids["weeks"][0])[0]
        match.a_golfer_id, match.a_is_sub, match.manually_adjusted = sub, True, True
        session.commit()
        candidates = card_candidates(session, ids["weeks"][0])
        found = {c.golfer_id for c in candidates}
        assert sub in found and outsider not in found
        assert len(candidates) == 8
        by_id = {c.golfer_id: c for c in candidates}
        assert by_id[sub].opponent_golfer_id == match.b_golfer_id
        assert by_id[match.b_golfer_id].opponent_name == by_id[sub].name
        assert session.get(Golfer, sub).first_name in by_id[sub].name


def test_week_holes_follow_the_nine_and_season_course(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        front = week_holes(session, ids["weeks"][0])
        back = week_holes(session, ids["weeks"][1])
        assert [h.number for h in front] == list(range(1, 10))
        assert [h.number for h in back] == list(range(10, 19))
        assert {h.nine for h in front} == {"front"}
        rain = Week(season_id=ids["season"], index=4, play_date=date(2026, 9, 17), nine=None,
                    week_type="rain_date", status="scheduled")
        session.add(rain)
        session.flush()
        assert week_holes(session, rain.id) == []
        assert week_holes(session, 999999) == []


def test_two_golfer_card_writes_eighteen_rows(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    with Session(engine) as session:
        match = _matches(session, ids["weeks"][0])[0]
        result = submit_card(session, ids["weeks"][0], _card(session, ids["weeks"][0], [match.a_golfer_id, match.b_golfer_id]),
                             user_id, TODAY, acknowledged=False)
        session.commit()
        assert result.rows_written == 18 and result.incomplete is False
        assert result.player_match_ids == (match.id,)
        rows = list(session.scalars(select(HoleScore)))
        assert len(rows) == 18
        assert {r.entered_by_user_id for r in rows} == {user_id}
        assert all(r.entered_at.tzinfo is not None for r in rows)
        pages = scores_for_week(session, ids["weeks"][0])
        assert len(pages) == 1 and pages[0].player_match_id == match.id
        assert [g.total for g in pages[0].golfers] == [72, 72] or [g.total for g in pages[0].golfers] == [36, 36]


def test_scores_for_week_derives_totals(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    with Session(engine) as session:
        match = _matches(session, ids["weeks"][0])[0]
        numbers = [h.number for h in week_holes(session, ids["weeks"][0])]
        entries = [(match.a_golfer_id, dict.fromkeys(numbers, 5)),
                   (match.b_golfer_id, {n: ("4" if n < 5 else 6) for n in numbers})]
        submit_card(session, ids["weeks"][0], entries, user_id, TODAY, acknowledged=False)
        totals = {g.golfer_id: g.total for g in scores_for_week(session, ids["weeks"][0])[0].golfers}
        assert totals == {match.a_golfer_id: 45, match.b_golfer_id: 4 * 4 + 5 * 6}


def test_four_golfer_card_across_two_matches_writes_thirty_six(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    with Session(engine) as session:
        m1, m2 = _matches(session, ids["weeks"][0])[:2]
        golfers = [m1.a_golfer_id, m1.b_golfer_id, m2.a_golfer_id, m2.b_golfer_id]
        result = submit_card(session, ids["weeks"][0], _card(session, ids["weeks"][0], golfers), user_id, TODAY,
                             acknowledged=False)
        assert result.rows_written == 36
        assert len(result.player_match_ids) == 2


def test_three_golfer_card_needs_acknowledgement(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    with Session(engine) as session:
        m1, m2 = _matches(session, ids["weeks"][0])[:2]
        golfers = [m1.a_golfer_id, m1.b_golfer_id, m2.a_golfer_id]
        card = _card(session, ids["weeks"][0], golfers)
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, ids["weeks"][0], card, user_id, TODAY, acknowledged=False)
        assert caught.value.code == "needs_acknowledgement"
        assert session.scalar(select(func.count(HoleScore.id))) == 0
        result = submit_card(session, ids["weeks"][0], card, user_id, TODAY, acknowledged=True)
        assert result.rows_written == 27 and result.incomplete is True


def _refusal(session, week_id, entries, user_id, today=TODAY, acknowledged=True):
    with pytest.raises(ScoreEntryError) as caught:
        submit_card(session, week_id, entries, user_id, today, acknowledged=acknowledged)
    assert session.scalar(select(func.count(HoleScore.id))) == 0
    return caught.value.code


def test_refusals_write_nothing(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        m1 = _matches(session, week_id)[0]
        a, b = m1.a_golfer_id, m1.b_golfer_id
        good = _card(session, week_id, [a, b])
        outsider = ids["golfers"][8]
        assert _refusal(session, week_id, _card(session, week_id, [a, outsider]), user_id) == "golfer_not_in_week"
        assert _refusal(session, week_id, _card(session, week_id, [a, a]), user_id) == "golfer_count"
        assert _refusal(session, week_id, _card(session, week_id, [a]), user_id) == "golfer_count"
        five = [m.a_golfer_id for m in _matches(session, week_id)] + [b]
        assert _refusal(session, week_id, _card(session, week_id, five), user_id) == "golfer_count"
        missing = {n: s for n, s in good[1][1].items() if n != 3}
        assert _refusal(session, week_id, [good[0], (b, missing)], user_id) == "missing_score"
        blank = dict(good[1][1])
        blank[2] = ""
        assert _refusal(session, week_id, [good[0], (b, blank)], user_id) == "missing_score"
        for bad in ("16", "0", "x", "٤", 4.5):
            scores = dict(good[1][1])
            scores[2] = bad
            assert _refusal(session, week_id, [good[0], (b, scores)], user_id) == "bad_score"
        extra = dict(good[1][1])
        extra[10] = 4
        assert _refusal(session, week_id, [good[0], (b, extra)], user_id) == "bad_score"
        assert _refusal(session, week_id, good, user_id, today=date(2026, 8, 28)) == "past_week"
        assert _refusal(session, ids["weeks"][1], good, user_id) == "no_matchup"
        assert _refusal(session, 999999, good, user_id) == "not_found"


def test_second_submit_and_shared_golfer_card_are_already_submitted(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        m1, m2 = _matches(session, week_id)[:2]
        first = _card(session, week_id, [m1.a_golfer_id, m1.b_golfer_id], value=3)
        submit_card(session, week_id, first, user_id, TODAY, acknowledged=False)
        session.commit()
        before = [(r.id, r.golfer_id, r.strokes) for r in session.scalars(select(HoleScore).order_by(HoleScore.id))]
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, _card(session, week_id, [m1.a_golfer_id, m1.b_golfer_id], value=6),
                        user_id, TODAY, acknowledged=False)
        assert caught.value.code == "already_submitted"
        session.rollback()
        overlap = _card(session, week_id, [m1.a_golfer_id, m2.a_golfer_id, m2.b_golfer_id], value=6)
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, overlap, user_id, TODAY, acknowledged=True)
        assert caught.value.code == "already_submitted"
        session.rollback()
        after = [(r.id, r.golfer_id, r.strokes) for r in session.scalars(select(HoleScore).order_by(HoleScore.id))]
        assert after == before


def test_unique_key_violation_becomes_already_submitted_without_poisoning(matchup_db, monkeypatch):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        m1 = _matches(session, week_id)[0]
        hole = session.scalar(select(Hole).where(Hole.number == 1))
        session.add(HoleScore(player_match_id=m1.id, golfer_id=m1.a_golfer_id, hole_id=hole.id, strokes=4,
                              entered_by_user_id=user_id))
        session.flush()
        # Blind the pre-check so the unique key and the savepoint path are what refuse the card.
        monkeypatch.setattr(scores_module, "_already_scored", lambda *args: False)
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, _card(session, week_id, [m1.a_golfer_id, m1.b_golfer_id]), user_id,
                        TODAY, acknowledged=False)
        assert caught.value.code == "already_submitted"
        # Caller's transaction is still usable and holds its earlier row.
        assert session.scalar(select(func.count(HoleScore.id))) == 1


def test_busy_writer_is_conflict_and_writes_nothing(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as caller, Session(engine) as writer:
        match = _matches(caller, week_id)[0]
        card = _card(caller, week_id, [match.a_golfer_id, match.b_golfer_id])
        caller.rollback()
        caller.connection().exec_driver_sql("PRAGMA busy_timeout=1")
        writer.connection().exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(caller, week_id, card, user_id, TODAY, acknowledged=False)
        assert caught.value.code == "conflict"
        caller.rollback()
        writer.rollback()
    assert _count(engine) == 0


def test_submit_does_not_commit(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        match = _matches(session, week_id)[0]
        card = _card(session, week_id, [match.a_golfer_id, match.b_golfer_id])
        submit_card(session, week_id, card, user_id, TODAY, acknowledged=False)
        assert _count(engine) == 0  # uncommitted rows are invisible to another connection
        session.rollback()
    assert _count(engine) == 0
    with Session(engine) as session:
        submit_card(session, week_id, card, user_id, TODAY, acknowledged=False)
        session.commit()
    assert _count(engine) == 18


def test_two_connections_racing_the_same_card_leave_one_card(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    barrier = Barrier(2)
    with Session(engine) as session:
        match = _matches(session, week_id)[0]
        golfers = [match.a_golfer_id, match.b_golfer_id]
        card = _card(session, week_id, golfers)
        session.rollback()

    def attempt(_):
        with Session(engine) as session:
            session.get(Week, week_id)  # existing read transaction before the reservation
            barrier.wait(timeout=10)
            try:
                submit_card(session, week_id, card, user_id, TODAY, acknowledged=False)
                session.commit()
                return True
            except ScoreEntryError as exc:
                session.rollback()
                assert exc.code in {"already_submitted", "conflict"}
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count(True) == 1
    assert _count(engine) == 18
