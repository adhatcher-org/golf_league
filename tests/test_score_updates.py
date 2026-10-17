"""GL-83: changing saved scores, who may, locking, unlocking, clearing and what a page shows.

Synthetic data only: the generated golfers `A1 Example` .. `D4 Example` and invented accounts.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_matchup_generation import clock as matchup_clock
from test_matchup_generation import create_generated
from test_matchup_generation import matchup_db as _matchup_db
from test_scores import _matches

from golf_league.domain.scorecard import Actor
from golf_league.models import (
    Golfer,
    HoleScore,
    MatchScoreState,
    SeasonGolfer,
    User,
    Week,
)
from golf_league.services.matchups import MatchupConflict, substitute_player
from golf_league.services.scores import (
    ScoreEntryError,
    blocked_golfers,
    clear_match_scores,
    kept_matches,
    lock_match,
    lock_week,
    player_entry_state,
    player_entry_states,
    save_match_scores,
    save_week,
    submit_card,
    unlock_match,
    week_holes,
    week_sheet,
    writable_golfers,
)

matchup_db = _matchup_db

TODAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)


@pytest.fixture
def db(matchup_db):
    """Week 1 generated (A against B: four matches) and week 3 generated; an admin and four players."""
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids, week_index=0)
        create_generated(session, ids, week_index=2)
        actors = {"admin": _account(session, "admin", None, admin=True)}
        m1, m2, m3 = _matches(session, ids["weeks"][0])[:3]
        # a1 and b1 play each other in match 1; a2 plays in match 2; a3 in match 3.
        for label, golfer_id in (("a1", m1.a_golfer_id), ("b1", m1.b_golfer_id), ("a2", m2.a_golfer_id),
                                 ("a3", m3.a_golfer_id)):
            actors[label] = _account(session, label, golfer_id)
        session.commit()
        ids = {**ids, "week": ids["weeks"][0], "m1": m1.id, "m2": m2.id, "m3": m3.id,
               "sides": {m.id: (m.a_golfer_id, m.b_golfer_id) for m in _matches(session, ids["weeks"][0])}}
    return engine, ids, actors


def _account(session, label, golfer_id, admin=False) -> Actor:
    user = User(username=f"{label}@example.test", email=f"{label}@example.test",
                display_name=f"Account {label}", password_hash="x")
    session.add(user)
    session.flush()
    return Actor(user.id, admin, golfer_id)


def _scores(session, week_id, golfer_ids, value=4):
    numbers = [hole.number for hole in week_holes(session, week_id)]
    return {golfer_id: dict.fromkeys(numbers, value) for golfer_id in golfer_ids}


def _save(session, ids, match_key, actor, base_revision, *, value=4, golfers=None, today=TODAY, now=NOW):
    match_id = ids[match_key]
    scores = _scores(session, ids["week"], golfers or ids["sides"][match_id], value)
    return save_match_scores(session, ids["week"], match_id, scores, actor, today,
                             base_revision=base_revision, now=now)


def _code(call) -> str:
    with pytest.raises(ScoreEntryError) as caught:
        call()
    return caught.value.code


def _rows(session, match_id=None):
    query = select(HoleScore).order_by(HoleScore.id)
    if match_id is not None:
        query = query.where(HoleScore.player_match_id == match_id)
    return [(r.id, r.golfer_id, r.strokes, r.entered_by_user_id, r.entered_at) for r in session.scalars(query)]


def _state(session, match_id):
    session.expire_all()
    return session.get(MatchScoreState, match_id)


# --- entering and changing one match ---------------------------------------------------


def test_a_first_entry_by_a_side_writes_both_golfers_and_starts_the_revision(db):
    engine, ids, actors = db
    with Session(engine) as session:
        result = _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        assert (result.revision, result.rows_inserted, result.rows_updated) == (1, 18, 0)
        assert {row[3] for row in _rows(session, ids["m1"])} == {actors["a1"].user_id}
        state = _state(session, ids["m1"])
        assert (state.revision, state.scores_changed_at, state.scores_changed_by_user_id) == (
            1, NOW, actors["a1"].user_id)
        assert state.locked_at is None and state.locked_by_user_id is None


def test_a_change_by_the_opponent_keeps_who_first_entered_and_raises_the_revision(db):
    engine, ids, actors = db
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        before = _rows(session, ids["m1"])
        a_side, b_side = ids["sides"][ids["m1"]]
        changed = _scores(session, ids["week"], [a_side, b_side])
        changed[a_side][3] = 7  # the opponent corrects one of a1's holes
        result = save_match_scores(session, ids["week"], ids["m1"], changed, actors["b1"], TODAY,
                                   base_revision=1, now=LATER)
        session.commit()
        assert (result.revision, result.rows_inserted, result.rows_updated) == (2, 0, 1)
        after = _rows(session, ids["m1"])
        # Same rows; only one stroke count differs; who entered and when are untouched.
        assert [(r[0], r[1], r[3], r[4]) for r in after] == [(r[0], r[1], r[3], r[4]) for r in before]
        assert sum(1 for old, new in zip(before, after, strict=True) if old[2] != new[2]) == 1
        state = _state(session, ids["m1"])
        assert (state.revision, state.scores_changed_at, state.scores_changed_by_user_id) == (
            2, LATER, actors["b1"].user_id)


def test_a_save_that_changes_nothing_writes_nothing_and_keeps_the_revision(db):
    engine, ids, actors = db
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        result = _save(session, ids, "m1", actors["b1"], 1, now=LATER)
        session.commit()
        assert (result.revision, result.rows_inserted, result.rows_updated) == (1, 0, 0)
        state = _state(session, ids["m1"])
        assert (state.revision, state.scores_changed_by_user_id) == (1, actors["a1"].user_id)


def test_an_out_of_date_save_writes_nothing(db):
    engine, ids, actors = db
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        before = _rows(session)
        for stale in (0, 2, 99, None):
            assert _code(lambda stale=stale: _save(session, ids, "m1", actors["b1"], stale, value=9)) == "out_of_date"
            session.rollback()
        # A first entry must also be based on "nothing saved yet".
        assert _code(lambda: _save(session, ids, "m2", actors["a2"], 1)) == "out_of_date"
        session.rollback()
        assert _rows(session) == before and _state(session, ids["m1"]).revision == 1


def test_missing_and_bad_scores_write_nothing_and_name_the_match(db):
    engine, ids, actors = db
    with Session(engine) as session:
        a_side, b_side = ids["sides"][ids["m1"]]
        full = _scores(session, ids["week"], [a_side, b_side])
        cases = {
            "missing_score": [{a_side: full[a_side]},  # the other golfer is absent
                              {a_side: full[a_side], b_side: {**full[b_side], 4: ""}},
                              {a_side: full[a_side], b_side: {k: v for k, v in full[b_side].items() if k != 4}}],
            "bad_score": [{a_side: full[a_side], b_side: {**full[b_side], 4: bad}} for bad in ("0", "16", "x", 4.5)]
                         + [{a_side: full[a_side], b_side: {**full[b_side], 10: 4}}],
        }
        for code, variants in cases.items():
            for scores in variants:
                with pytest.raises(ScoreEntryError) as caught:
                    save_match_scores(session, ids["week"], ids["m1"], scores, actors["a1"], TODAY,
                                      base_revision=0, now=NOW)
                assert (caught.value.code, caught.value.player_match_id) == (code, ids["m1"])
                session.rollback()
        assert _rows(session) == [] and _state(session, ids["m1"]) is None


def test_the_week_and_the_match_are_checked(db):
    engine, ids, actors = db
    other_week = ids["weeks"][2]
    with Session(engine) as session:
        other_match = _matches(session, other_week)[0].id
        # A match from another week, and a match that does not exist.
        for match_id in (other_match, 999999):
            assert _code(lambda match_id=match_id: save_match_scores(
                session, ids["week"], match_id, {}, actors["admin"], TODAY, base_revision=0, now=NOW)) == "not_found"
            session.rollback()
        # Before the play date a player cannot enter; an admin can.
        early = date(2026, 8, 26)
        assert _code(lambda: _save(session, ids, "m1", actors["a1"], 0, today=early)) == "not_open_yet"
        session.rollback()
        assert _save(session, ids, "m1", actors["admin"], 0, today=early).rows_inserted == 18
        session.rollback()
        # Long after the play date a player still can: the lock is the only cutoff.
        assert _save(session, ids, "m1", actors["a1"], 0, today=date(2026, 12, 1)).rows_inserted == 18
        session.rollback()
        session.get(Week, ids["week"]).status = "cancelled"
        session.commit()
        assert _code(lambda: _save(session, ids, "m1", actors["admin"], 0)) == "cancelled"


# --- who may write ----------------------------------------------------------------------


def test_only_the_sides_the_scorekeeper_and_an_admin_may_write(db):
    engine, ids, actors = db
    stranger, unlinked = actors["a3"], Actor(actors["a3"].user_id, False, None)
    with Session(engine) as session:
        match = session.get(type(_matches(session, ids["week"])[0]), ids["m1"])
        sides = frozenset(ids["sides"][ids["m1"]])
        assert writable_golfers(session, match, actors["admin"]) == sides
        assert writable_golfers(session, match, actors["a1"]) == sides
        assert writable_golfers(session, match, actors["b1"]) == sides
        for nobody in (actors["a2"], stranger, unlinked):
            assert writable_golfers(session, match, nobody) == frozenset()
            assert _code(lambda nobody=nobody: _save(session, ids, "m1", nobody, 0)) == "not_allowed"
            session.rollback()
        assert _rows(session) == []


def test_a_scorekeeper_may_change_only_the_golfers_they_first_entered(db):
    engine, ids, actors = db
    keeper = actors["a2"]  # plays in match 2, keeps score for match 1 on a four-golfer card
    m1_a, m1_b = ids["sides"][ids["m1"]]
    m2_a, m2_b = ids["sides"][ids["m2"]]
    with Session(engine) as session:
        card = [(g, _scores(session, ids["week"], [g])[g]) for g in (m2_a, m2_b, m1_a, m1_b)]
        result = submit_card(session, ids["week"], card, keeper, TODAY, acknowledged=False, now=NOW)
        session.commit()
        assert result.rows_written == 36 and kept_matches(session, ids["week"], keeper) == [ids["m1"]]

        match_one = next(m for m in _matches(session, ids["week"]) if m.id == ids["m1"])
        assert writable_golfers(session, match_one, keeper) == frozenset({m1_a, m1_b})
        assert _save(session, ids, "m1", keeper, 1, value=6).rows_updated == 18
        session.commit()
        # The players of match 1 can change their own match too, and it stays the keeper's entry.
        assert _save(session, ids, "m1", actors["b1"], 2, value=5).rows_updated == 18
        session.commit()
        assert {row[3] for row in _rows(session, ids["m1"])} == {keeper.user_id}
        assert writable_golfers(session, match_one, keeper) == frozenset({m1_a, m1_b})
        # A third player has no rights over either match.
        assert writable_golfers(session, match_one, actors["a3"]) == frozenset()
        assert _code(lambda: _save(session, ids, "m2", actors["a3"], 1, value=9)) == "not_allowed"


def test_a_scorekeeper_loses_the_right_when_no_longer_a_linked_golfer_in_the_week(db):
    engine, ids, actors = db
    keeper = actors["a2"]
    m1_a, m1_b = ids["sides"][ids["m1"]]
    m2_a, m2_b = ids["sides"][ids["m2"]]
    with Session(engine) as session:
        card = [(g, _scores(session, ids["week"], [g])[g]) for g in (m2_a, m2_b, m1_a, m1_b)]
        submit_card(session, ids["week"], card, keeper, TODAY, acknowledged=False, now=NOW)
        session.commit()
        before = _rows(session)
        match_one = next(m for m in _matches(session, ids["week"]) if m.id == ids["m1"])
        in_week = {golfer_id for sides in ids["sides"].values() for golfer_id in sides}
        outsider = next(g for g in session.scalars(select(Golfer.id).order_by(Golfer.id)) if g not in in_week)
        # The same account with its golfer unlinked, or linked to a golfer who does not play that week.
        for former in (Actor(keeper.user_id, False, None), Actor(keeper.user_id, False, outsider)):
            assert writable_golfers(session, match_one, former) == frozenset()
            for match_key in ("m1", "m2"):
                assert _code(lambda former=former, match_key=match_key:
                             _save(session, ids, match_key, former, 1, value=9)) == "not_allowed"
                session.rollback()
        assert _rows(session) == before
        # Linked and playing in the week, the keeper still has the right.
        assert _save(session, ids, "m1", keeper, 1, value=6).rows_updated == 18


def test_a_week_that_is_not_a_match_week_takes_no_scores_even_with_matches(db):
    engine, ids, actors = db
    with Session(engine) as session:
        session.get(Week, ids["week"]).week_type = "play_with_team"
        session.commit()
        for actor in (actors["admin"], actors["a1"]):
            assert _code(lambda actor=actor: _save(session, ids, "m1", actor, 0)) == "no_matchup"
            session.rollback()
        entries = [(g, _scores(session, ids["week"], [g])[g]) for g in ids["sides"][ids["m1"]]]
        assert _code(lambda: submit_card(session, ids["week"], entries, actors["admin"], TODAY,
                                         acknowledged=False, now=NOW)) == "no_matchup"
        session.rollback()
        assert _rows(session) == []


def test_a_scorekeeper_has_no_right_over_a_golfer_someone_else_entered(db):
    engine, ids, actors = db
    keeper = actors["a2"]
    m1_a, m1_b = ids["sides"][ids["m1"]]
    m2_a, m2_b = ids["sides"][ids["m2"]]
    with Session(engine) as session:
        # a1 types only their own nine first... which needs both golfers, so the admin enters a1 alone
        # through a card with a golfer of match 3.
        m3_a = ids["sides"][ids["m3"]][0]
        admin_card = [(g, _scores(session, ids["week"], [g])[g]) for g in (m1_a, m3_a)]
        submit_card(session, ids["week"], admin_card, actors["admin"], TODAY, acknowledged=True, now=NOW)
        session.commit()
        # The keeper's four-golfer card leaves a1 off and adds b1 and their own match.
        card = [(g, _scores(session, ids["week"], [g])[g]) for g in (m2_a, m2_b, m1_a, m1_b)]
        assert _code(lambda: submit_card(session, ids["week"], card, keeper, TODAY, acknowledged=False,
                                         now=NOW)) == "left_off"
        session.rollback()
        submit_card(session, ids["week"], card, keeper, TODAY, acknowledged=False,
                    left_off=frozenset({m1_a}), now=NOW)
        session.commit()
        match_one = next(m for m in _matches(session, ids["week"]) if m.id == ids["m1"])
        assert writable_golfers(session, match_one, keeper) == frozenset({m1_b})
        # A save by the keeper changes b1 and ignores what was posted for a1.
        before_a = [row for row in _rows(session, ids["m1"]) if row[1] == m1_a]
        result = save_match_scores(session, ids["week"], ids["m1"],
                                   _scores(session, ids["week"], [m1_a, m1_b], 9), keeper, TODAY,
                                   base_revision=_state(session, ids["m1"]).revision, now=LATER)
        session.commit()
        assert result.rows_updated == 9
        assert [row for row in _rows(session, ids["m1"]) if row[1] == m1_a] == before_a


@pytest.mark.parametrize("case,message", [
    ("own golfer missing", "Your own name must be on the card."),
    ("a third match", "A card can hold your match and one other match."),
    ("a second other match in the week", "You have already kept score for another match this week."),
])
def test_a_players_card_holds_their_own_match_and_one_other_a_week(db, case, message):
    engine, ids, actors = db
    keeper = actors["a2"]
    m1, m2, m3 = (ids["sides"][ids[key]] for key in ("m1", "m2", "m3"))
    with Session(engine) as session:
        def card(*golfers):
            return [(g, _scores(session, ids["week"], [g])[g]) for g in golfers]

        if case == "own golfer missing":
            entries, ack = card(*m1), False
        elif case == "a third match":
            entries, ack = card(m2[0], m2[1], m1[0], m3[0]), True
        else:
            submit_card(session, ids["week"], card(*m2, *m1), keeper, TODAY, acknowledged=False, now=NOW)
            session.commit()
            entries, ack = card(m2[0], *m3), True  # own golfer plus a different other match
        before = _rows(session)
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, ids["week"], entries, keeper, TODAY, acknowledged=ack, now=NOW,
                        left_off=blocked_golfers(session, ids["week"], [g for g, _ in entries]))
        assert (caught.value.code, caught.value.message) == ("card_scope", message)
        session.rollback()
        assert _rows(session) == before


def test_a_player_card_rules_do_not_bind_an_admin_and_an_unlinked_account_writes_nothing(db):
    engine, ids, actors = db
    m1, m2, m3 = (ids["sides"][ids[key]] for key in ("m1", "m2", "m3"))
    with Session(engine) as session:
        entries = [(g, _scores(session, ids["week"], [g])[g]) for g in (m1[0], m2[0], m3[0])]
        unlinked = Actor(actors["a3"].user_id, False, None)
        assert _code(lambda: submit_card(session, ids["week"], entries, unlinked, TODAY, acknowledged=True,
                                         now=NOW)) == "not_allowed"
        session.rollback()
        # An admin's card may span three matches and need not include the admin.
        result = submit_card(session, ids["week"], entries, actors["admin"], TODAY, acknowledged=True, now=NOW)
        session.commit()
        assert result.rows_written == 27
        # Keeping score for one other match does not stop a later card for the player's own match only.
        keeper = actors["a2"]
        assert kept_matches(session, ids["week"], keeper) == []
        own = [(g, _scores(session, ids["week"], [g])[g]) for g in m2]
        assert _code(lambda: submit_card(session, ids["week"], own, keeper, TODAY, acknowledged=False,
                                         now=NOW)) == "left_off"  # the admin already entered m2's first golfer


# --- lock and unlock --------------------------------------------------------------------


def test_a_locked_match_refuses_every_write_by_anyone_until_unlocked(db):
    engine, ids, actors = db
    admin = actors["admin"]
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        assert lock_match(session, ids["week"], ids["m1"], admin, base_revision=1, now=LATER) == 2
        session.commit()
        state = _state(session, ids["m1"])
        assert (state.locked_at, state.locked_by_user_id) == (LATER, admin.user_id)
        assert state.scores_changed_by_user_id == actors["a1"].user_id  # a lock is not a change to the scores
        before = _rows(session)

        for actor in (actors["a1"], actors["b1"], admin):
            assert _code(lambda actor=actor: _save(session, ids, "m1", actor, 2, value=9)) == "locked"
            session.rollback()
        assert _code(lambda: clear_match_scores(session, ids["week"], ids["m1"], admin, base_revision=2,
                                                now=LATER)) == "locked"
        session.rollback()
        card = [(g, _scores(session, ids["week"], [g], 9)[g]) for g in ids["sides"][ids["m1"]]]
        assert _code(lambda: submit_card(session, ids["week"], card, admin, TODAY, acknowledged=False,
                                         now=LATER)) == "nothing_left"
        session.rollback()
        assert _rows(session) == before
        # Locking again changes nothing.
        assert lock_match(session, ids["week"], ids["m1"], admin, base_revision=0, now=LATER) == 2

        assert unlock_match(session, ids["week"], ids["m1"], admin, now=LATER) == 3
        session.commit()
        state = _state(session, ids["m1"])
        assert state.locked_at is None and state.locked_by_user_id is None
        assert unlock_match(session, ids["week"], ids["m1"], admin, now=LATER) == 3  # not locked: no change
        assert _save(session, ids, "m1", actors["b1"], 3, value=6).rows_updated == 18


def test_a_lock_needs_an_admin_the_right_week_and_an_unchanged_match(db):
    engine, ids, actors = db
    admin = actors["admin"]
    with Session(engine) as session:
        for player in (actors["a1"], Actor(admin.user_id, False, None)):
            assert _code(lambda player=player: lock_match(session, ids["week"], ids["m1"], player,
                                                          base_revision=0, now=NOW)) == "not_allowed"
            assert _code(lambda player=player: unlock_match(session, ids["week"], ids["m1"], player,
                                                            now=NOW)) == "not_allowed"
            assert _code(lambda player=player: lock_week(session, ids["week"], player, {}, now=NOW)) == "not_allowed"
            assert _code(lambda player=player: clear_match_scores(session, ids["week"], ids["m1"], player,
                                                                  base_revision=0, now=NOW)) == "not_allowed"
        other_week_match = _matches(session, ids["weeks"][2])[0].id
        assert _code(lambda: lock_match(session, ids["week"], other_week_match, admin, base_revision=0,
                                        now=NOW)) == "not_found"
        assert _code(lambda: unlock_match(session, ids["week"], 999999, admin, now=NOW)) == "not_found"
        session.rollback()
        _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        # The admin's page showed the match as empty; it has scores now.
        assert _code(lambda: lock_match(session, ids["week"], ids["m1"], admin, base_revision=0,
                                        now=NOW)) == "out_of_date"
        session.rollback()
        assert _state(session, ids["m1"]).locked_at is None


def test_an_empty_match_can_be_locked_and_then_takes_no_entry(db):
    engine, ids, actors = db
    with Session(engine) as session:
        assert lock_match(session, ids["week"], ids["m3"], actors["admin"], base_revision=0, now=NOW) == 1
        session.commit()
        state = _state(session, ids["m3"])
        assert state.locked_at == NOW and state.scores_changed_at is None
        assert _code(lambda: _save(session, ids, "m3", actors["a3"], 1)) == "locked"
        session.rollback()
        # Lock, unlock and clear need only the match to belong to the week, whatever the week's state.
        session.get(Week, ids["week"]).status = "cancelled"
        session.commit()
        assert unlock_match(session, ids["week"], ids["m3"], actors["admin"], now=LATER) == 2


def test_lock_all_locks_complete_unchanged_matches_and_reports_the_rest(db):
    engine, ids, actors = db
    admin = actors["admin"]
    with Session(engine) as session:
        match_ids = [m.id for m in _matches(session, ids["week"])]
        m4 = match_ids[3]
        _save(session, ids, "m1", actors["a1"], 0)            # complete: will be locked
        _save(session, ids, "m2", actors["a2"], 0)            # complete, then changed after the page
        lock_match(session, ids["week"], m4, admin, base_revision=0, now=NOW)  # already locked, empty
        session.commit()
        shown = {match.player_match_id: match.revision for match in week_sheet(session, ids["week"])}
        _save(session, ids, "m2", actors["a2"], 1, value=6, now=LATER)  # someone changes match 2
        session.commit()

        result = lock_week(session, ids["week"], admin, shown, now=LATER)
        session.commit()
        assert result.locked == (ids["m1"],)
        assert result.changed == (ids["m2"],)
        assert result.incomplete == (ids["m3"],)  # nothing entered: left open
        assert result.already_locked == (m4,)
        assert _state(session, ids["m1"]).locked_at == LATER
        assert _state(session, ids["m2"]).locked_at is None and _state(session, ids["m3"]) is None
        # A match missing from what the page showed is treated as changed, never locked blind.
        again = lock_week(session, ids["week"], admin, {}, now=LATER)
        assert again.locked == () and again.changed == (ids["m2"],)
        assert _code(lambda: lock_week(session, 999999, admin, {}, now=LATER)) == "not_found"


def test_lock_all_leaves_a_half_entered_match_open(db):
    engine, ids, actors = db
    m1_a, m3_a = ids["sides"][ids["m1"]][0], ids["sides"][ids["m3"]][0]
    with Session(engine) as session:
        card = [(g, _scores(session, ids["week"], [g])[g]) for g in (m1_a, m3_a)]
        submit_card(session, ids["week"], card, actors["admin"], TODAY, acknowledged=True, now=NOW)
        session.commit()
        shown = {match.player_match_id: match.revision for match in week_sheet(session, ids["week"])}
        result = lock_week(session, ids["week"], actors["admin"], shown, now=LATER)
        assert result.locked == () and set(result.incomplete) == {m.id for m in _matches(session, ids["week"])}


# --- clearing ---------------------------------------------------------------------------


def test_clear_deletes_one_matchs_scores_and_entry_and_substitution_work_again(db):
    engine, ids, actors = db
    admin = actors["admin"]
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        _save(session, ids, "m2", actors["a2"], 0)
        replacement = Golfer(first_name="Replacement", last_name="Synthetic", default_tee_set_id=ids["tee"],
                             handicap_strokes=2, handicap_source="self_reported", handicap_status="ok")
        session.add(replacement)
        session.flush()
        session.add(SeasonGolfer(season_id=ids["season"], golfer_id=replacement.id))
        session.commit()
        with pytest.raises(MatchupConflict, match="submitted scores"):
            substitute_player(session, ids["season"], ids["m1"], side="a", golfer_id=replacement.id,
                              clock=matchup_clock)
        session.rollback()
        match_two = _rows(session, ids["m2"])

        assert _code(lambda: clear_match_scores(session, ids["week"], ids["m1"], admin, base_revision=0,
                                                now=LATER)) == "out_of_date"
        session.rollback()
        assert clear_match_scores(session, ids["week"], ids["m1"], admin, base_revision=1, now=LATER) == 18
        session.commit()
        assert _rows(session, ids["m1"]) == [] and _rows(session, ids["m2"]) == match_two
        state = _state(session, ids["m1"])
        assert (state.revision, state.scores_changed_at, state.scores_changed_by_user_id) == (
            2, LATER, admin.user_id)
        # Nothing to clear: no change.
        assert clear_match_scores(session, ids["week"], ids["m1"], admin, base_revision=2, now=LATER) == 0
        assert _state(session, ids["m1"]).revision == 2

        substitute_player(session, ids["season"], ids["m1"], side="a", golfer_id=replacement.id,
                          clock=matchup_clock)
        session.commit()
        new_sides = (replacement.id, ids["sides"][ids["m1"]][1])
        result = save_match_scores(session, ids["week"], ids["m1"], _scores(session, ids["week"], new_sides),
                                   actors["b1"], TODAY, base_revision=2, now=LATER)
        assert (result.revision, result.rows_inserted) == (3, 18)


# --- the admin week sheet in one save ----------------------------------------------------


def test_save_week_saves_every_match_given_or_none(db):
    engine, ids, actors = db
    admin = actors["admin"]
    with Session(engine) as session:
        def entry(key, revision, value=4):
            return (ids[key], _scores(session, ids["week"], ids["sides"][ids[key]], value), revision)

        assert _code(lambda: save_week(session, ids["week"], [entry("m1", 0)], actors["a1"], TODAY,
                                       now=NOW)) == "not_allowed"
        session.rollback()
        results = save_week(session, ids["week"], [entry("m1", 0), entry("m2", 0)], admin, TODAY, now=NOW)
        session.commit()
        assert [(r.player_match_id, r.revision, r.rows_inserted) for r in results] == [
            (ids["m1"], 1, 18), (ids["m2"], 1, 18)]
        before = _rows(session)

        # The third match is fine, but match 1's revision is out of date: nothing at all is saved.
        with pytest.raises(ScoreEntryError) as caught:
            save_week(session, ids["week"], [entry("m3", 0), entry("m1", 0, value=9)], admin, TODAY, now=LATER)
        assert (caught.value.code, caught.value.player_match_id) == ("out_of_date", ids["m1"])
        session.rollback()
        assert _rows(session) == before and _state(session, ids["m3"]) is None

        lock_match(session, ids["week"], ids["m2"], admin, base_revision=1, now=LATER)
        session.commit()
        with pytest.raises(ScoreEntryError) as caught:
            save_week(session, ids["week"], [entry("m3", 0), entry("m2", 2, value=9)], admin, TODAY, now=LATER)
        assert (caught.value.code, caught.value.player_match_id) == ("locked", ids["m2"])
        session.rollback()
        assert _rows(session) == before


# --- racing writers ---------------------------------------------------------------------


def test_two_saves_from_the_same_revision_leave_one_winner(db):
    engine, ids, actors = db
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        scores = {value: _scores(session, ids["week"], ids["sides"][ids["m1"]], value) for value in (6, 7)}
    barrier = Barrier(2)

    def attempt(item):
        actor, value = item
        with Session(engine) as session:
            session.get(Week, ids["week"])  # an existing read transaction before the reservation
            barrier.wait(timeout=10)
            try:
                save_match_scores(session, ids["week"], ids["m1"], scores[value], actor, TODAY,
                                  base_revision=1, now=LATER)
                session.commit()
                return value
            except ScoreEntryError as exc:
                session.rollback()
                assert exc.code in {"out_of_date", "conflict"}
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [(actors["a1"], 6), (actors["b1"], 7)]))
    winners = [value for value in results if value is not None]
    assert len(winners) == 1
    with Session(engine) as session:
        assert {row[2] for row in _rows(session, ids["m1"])} == {winners[0]}
        assert session.get(MatchScoreState, ids["m1"]).revision == 2


def test_a_save_racing_a_lock_never_writes_into_a_locked_match(db):
    engine, ids, actors = db
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        session.commit()
        scores = _scores(session, ids["week"], ids["sides"][ids["m1"]], 9)
    barrier = Barrier(2)

    def save(_):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            try:
                save_match_scores(session, ids["week"], ids["m1"], scores, actors["b1"], TODAY,
                                  base_revision=1, now=LATER)
                session.commit()
                return "saved"
            except ScoreEntryError as exc:
                session.rollback()
                return exc.code

    def lock(_):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            try:
                lock_match(session, ids["week"], ids["m1"], actors["admin"], base_revision=1, now=LATER)
                session.commit()
                return "locked"
            except ScoreEntryError as exc:
                session.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        saved, locked = pool.submit(save, None), pool.submit(lock, None)
        outcome = (saved.result(), locked.result())
    with Session(engine) as session:
        state = session.get(MatchScoreState, ids["m1"])
        strokes = {row[2] for row in _rows(session, ids["m1"])}
        # Either the save won and the lock was told the match changed, or the lock won and the save was refused.
        assert outcome in {("saved", "out_of_date"), ("locked", "locked"), ("out_of_date", "locked"),
                           ("conflict", "locked"), ("saved", "conflict")}
        if state.locked_at is not None:
            assert strokes == {4}  # locked as the admin saw it
        else:
            assert strokes == {9}


# --- what a page shows ------------------------------------------------------------------


def test_the_week_sheet_lists_every_match_in_order_with_scores_change_and_lock(db):
    engine, ids, actors = db
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0, value=5)
        lock_match(session, ids["week"], ids["m1"], actors["admin"], base_revision=1, now=LATER)
        m2_a = ids["sides"][ids["m2"]][0]
        m3_a = ids["sides"][ids["m3"]][0]
        card = [(g, _scores(session, ids["week"], [g], 3)[g]) for g in (m2_a, m3_a)]
        submit_card(session, ids["week"], card, actors["admin"], TODAY, acknowledged=True, now=NOW)
        session.commit()

        sheet = week_sheet(session, ids["week"])
        assert [match.player_match_id for match in sheet] == [m.id for m in _matches(session, ids["week"])]
        first, second, _, fourth = sheet
        assert (first.home_team_name, first.away_team_name, first.position_label) == ("A", "B", "1")
        assert [golfer.name for golfer in first.golfers] == ["A1 Example", "B1 Example"]
        assert [golfer.golfer_id for golfer in first.golfers] == list(ids["sides"][ids["m1"]])
        assert (first.revision, first.locked, first.locked_at, first.locked_by_name) == (
            2, True, LATER, "Account admin")
        assert (first.scores_changed_at, first.scores_changed_by_name) == (NOW, "Account a1")
        assert first.complete and not first.empty
        assert first.golfers[0].strokes_by_hole == dict.fromkeys(range(1, 10), 5)
        assert (first.golfers[0].total, first.golfers[0].entered_by_name) == (45, "Account a1")
        # One golfer entered: neither complete nor empty.
        assert not second.complete and not second.empty and not second.locked
        assert (second.golfers[0].total, second.golfers[1].total) == (27, None)
        assert second.golfers[1].strokes_by_hole == {} and second.golfers[1].entered_by_user_id is None
        # Nothing entered and no state row: revision 0.
        assert (fourth.revision, fourth.empty, fourth.complete, fourth.locked) == (0, True, False, False)
        assert fourth.scores_changed_at is None and fourth.locked_by_name is None
        assert week_sheet(session, 999999) == []


def test_the_week_sheet_never_shows_a_revision_newer_than_its_scores(db, monkeypatch):
    engine, ids, actors = db
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0, value=4)
        session.commit()  # revision 1: fours
    import golf_league.services.scores as scores_module

    original, saved_elsewhere = scores_module._rows_by_golfer, []

    def rows_then_a_save_elsewhere(session, match_id, **options):
        rows = original(session, match_id, **options)
        if match_id == ids["m1"] and not saved_elsewhere:  # someone saves sixes right after the read
            saved_elsewhere.append(True)
            with Session(engine) as other:
                _save(other, ids, "m1", actors["b1"], 1, value=6)
                other.commit()  # revision 2: sixes
        return rows

    monkeypatch.setattr(scores_module, "_rows_by_golfer", rows_then_a_save_elsewhere)
    with Session(engine) as session:
        shown = next(m for m in week_sheet(session, ids["week"]) if m.player_match_id == ids["m1"])
    monkeypatch.undo()
    assert saved_elsewhere
    assert shown.revision == 1 and all(set(g.strokes_by_hole.values()) == {4} for g in shown.golfers)
    # Saving what that page showed, on the revision it showed, is refused; the sixes stay.
    with Session(engine) as session:
        assert _code(lambda: _save(session, ids, "m1", actors["a1"], shown.revision, value=4)) == "out_of_date"
        session.rollback()
        assert {row[2] for row in _rows(session, ids["m1"])} == {6}


def test_player_entry_state_covers_every_state(db):
    engine, ids, actors = db
    a1 = actors["a1"].golfer_id
    week = ids["week"]

    def state(session, golfer_id=a1, today=TODAY, week_id=week):
        return player_entry_state(session, week_id, golfer_id, today).state

    with Session(engine) as session:
        outsider = ids["golfers"][8]  # a team C golfer: not in week 1's matchups
        assert state(session, None) == "not_playing"
        assert state(session, outsider) == "not_playing"
        assert state(session, week_id=999999) == "not_playing"
        early = player_entry_state(session, week, a1, date(2026, 8, 26))
        assert (early.state, early.player_match_id, early.opens_on) == ("not_open_yet", ids["m1"], TODAY)
        assert state(session) == "open" and state(session, today=date(2026, 12, 1)) == "open"

        # One golfer entered is still open; both entered is saved.
        m3_a = ids["sides"][ids["m3"]][0]
        card = [(g, _scores(session, week, [g])[g]) for g in (a1, m3_a)]
        submit_card(session, week, card, actors["admin"], TODAY, acknowledged=True, now=NOW)
        session.commit()
        assert state(session) == "open"
        b1 = actors["b1"].golfer_id
        save_match_scores(session, week, ids["m1"], _scores(session, week, [a1, b1]), actors["b1"], TODAY,
                          base_revision=1, now=NOW)
        session.commit()
        assert state(session) == "saved" and state(session, b1) == "saved"

        lock_match(session, week, ids["m1"], actors["admin"], base_revision=2, now=LATER)
        session.commit()
        assert state(session) == "locked"
        assert player_entry_states(session, [week, ids["weeks"][2], 999999], a1, TODAY) == {
            week: player_entry_state(session, week, a1, TODAY),
            ids["weeks"][2]: player_entry_state(session, ids["weeks"][2], a1, TODAY),
            999999: player_entry_state(session, 999999, a1, TODAY),
        }
        assert state(session, week_id=ids["weeks"][2]) == "not_open_yet"  # 10 September
        for status in ("cancelled", "played"):
            session.get(Week, week).status = status
            session.flush()
            assert state(session) == "closed"
        session.rollback()
        # A week whose type changed while its matches remained takes no scores (as `week_for_entry`).
        session.get(Week, week).week_type = "play_with_team"
        session.flush()
        assert state(session) == "closed"
        session.rollback()


def test_no_score_service_commits(db):
    engine, ids, actors = db
    admin = actors["admin"]
    with Session(engine) as session:
        _save(session, ids, "m1", actors["a1"], 0)
        lock_match(session, ids["week"], ids["m1"], admin, base_revision=1, now=NOW)
        unlock_match(session, ids["week"], ids["m1"], admin, now=NOW)
        clear_match_scores(session, ids["week"], ids["m1"], admin, base_revision=3, now=NOW)
        _save(session, ids, "m2", actors["a2"], 0)
        lock_week(session, ids["week"], admin, {ids["m2"]: 1}, now=NOW)
        session.rollback()  # the caller owns the transaction
    with Session(engine) as session:
        assert session.scalar(select(func.count(HoleScore.id))) == 0
        assert session.scalar(select(func.count(MatchScoreState.player_match_id))) == 0
