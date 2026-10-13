"""GL-79 score storage and the submit service (synthetic data only)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_matchup_generation import clock as matchup_clock
from test_matchup_generation import create_generated
from test_matchup_generation import matchup_db as _matchup_db

import golf_league.services.scores as scores_module
from golf_league.domain.scorecard import Actor
from golf_league.models import (
    Course,
    Golfer,
    Hole,
    HoleScore,
    HoleYardage,
    MatchScoreState,
    PlayerMatch,
    Season,
    SeasonGolfer,
    TeamMatch,
    TeamMember,
    User,
    Week,
)
from golf_league.services.courses import CourseValidationError, save_hole_grid
from golf_league.services.matchups import (
    MatchupConflict,
    generate_week_matches,
    substitute_player,
)
from golf_league.services.schedule import ScheduleConflict, confirm_nine, preview_nine
from golf_league.services.scores import (
    ScoreEntryError,
    card_candidates,
    default_week,
    league_today,
    week_holes,
    week_sheet,
)
from golf_league.services.seasons import SeasonValidationError, update_season

TODAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
ANY_ADMIN = Actor(0, True)


def submit_card(session, week_id, entries, user_id, today, *, acknowledged, left_off=frozenset()):
    """An admin's card, as every M8 test here submits one."""
    return scores_module.submit_card(session, week_id, entries, Actor(user_id, True), today,
                                     acknowledged=acknowledged, left_off=left_off, now=NOW)


def week_for_entry(session, week_id, today, actor=ANY_ADMIN):
    return scores_module.week_for_entry(session, week_id, today, actor)


def eligible_weeks(session, season_id, today, actor=ANY_ADMIN):
    return scores_module.eligible_weeks(session, season_id, today, actor)

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
    player = Actor(1, False, ids["golfers"][0])
    with Session(engine) as session:
        # week 2 has no matches; add an unactivated rain date without matches.
        session.add(Week(season_id=ids["season"], index=4, play_date=date(2026, 9, 17), nine=None,
                         week_type="rain_date", status="scheduled"))
        session.commit()
        first, third = ids["weeks"][0], ids["weeks"][2]  # 27 August and 10 September
        # An admin has no date limit; a player sees only weeks whose play date has come.
        for today in (TODAY, date(2026, 12, 1)):
            assert [w.id for w in eligible_weeks(session, ids["season"], today)] == [first, third]
        assert [w.id for w in eligible_weeks(session, ids["season"], TODAY, player)] == [first]
        assert eligible_weeks(session, ids["season"], date(2026, 8, 26), player) == []
        assert [w.id for w in eligible_weeks(session, ids["season"], date(2026, 9, 10), player)] == [first, third]

        # The default: today's week; then a week of the last six days that still has an open match;
        # then the next week; a week more than six days back is never the default.
        assert default_week(session, ids["season"], TODAY).id == first
        assert default_week(session, ids["season"], date(2026, 8, 28)).id == first
        assert default_week(session, ids["season"], date(2026, 9, 2)).id == first
        assert default_week(session, ids["season"], date(2026, 9, 3)).id == third
        assert default_week(session, ids["season"], date(2026, 8, 20)).id == first
        assert default_week(session, ids["season"], date(2026, 9, 20)) is None
        assert [w.id for w in eligible_weeks(session, ids["season"], date(2026, 9, 20))] == [first, third]

        # Cancelled and played weeks are never eligible.
        session.get(Week, first).status = "cancelled"
        session.get(Week, third).status = "played"
        session.commit()
        assert eligible_weeks(session, ids["season"], TODAY) == []
        assert default_week(session, ids["season"], TODAY) is None


def test_default_week_skips_a_recent_week_with_nothing_left_to_enter(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids, extra_weeks=(2,))
    first, third = ids["weeks"][0], ids["weeks"][2]
    admin = Actor(user_id, True)
    with Session(engine) as session:
        matches = _matches(session, first)
        # Every match but the last is saved; the last is locked empty, so nothing is open.
        for match in matches[:-1]:
            submit_card(session, first, _card(session, first, [match.a_golfer_id, match.b_golfer_id]),
                        user_id, TODAY, acknowledged=False)
        session.commit()
        assert default_week(session, ids["season"], date(2026, 8, 28)).id == first  # one match still open
        scores_module.lock_match(session, first, matches[-1].id, admin, base_revision=0, now=NOW)
        session.commit()
        assert default_week(session, ids["season"], date(2026, 8, 28)).id == third


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
    player = Actor(1, False, ids["golfers"][0])
    with Session(engine) as session:
        assert week_for_entry(session, ids["weeks"][0], TODAY).id == ids["weeks"][0]
        def code(week_id, today=TODAY, actor=ANY_ADMIN):
            with pytest.raises(ScoreEntryError) as caught:
                week_for_entry(session, week_id, today, actor)
            return caught.value.code
        assert code(999999) == "not_found"
        assert code(ids["weeks"][1]) == "no_matchup"
        # There is no past-week refusal: yesterday's week, and one long past, are accepted.
        for today in (date(2026, 8, 28), date(2026, 12, 1)):
            assert week_for_entry(session, ids["weeks"][0], today).id == ids["weeks"][0]
            assert week_for_entry(session, ids["weeks"][0], today, player).id == ids["weeks"][0]
        # Before the play date an admin may enter and a player may not.
        assert week_for_entry(session, ids["weeks"][0], date(2026, 8, 26)).id == ids["weeks"][0]
        assert code(ids["weeks"][0], date(2026, 8, 26), player) == "not_open_yet"
        week = session.get(Week, ids["weeks"][0])
        week.status = "cancelled"
        assert code(week.id) == "cancelled"
        assert code(week.id, date(2026, 8, 26), player) == "cancelled"  # the week's state is checked first
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
        pages = [page for page in week_sheet(session, ids["weeks"][0]) if not page.empty]
        assert len(pages) == 1 and pages[0].player_match_id == match.id
        assert [g.total for g in pages[0].golfers] == [36, 36]


def test_scored_week_refuses_nine_change_even_when_card_arrives_after_preview(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        preview = preview_nine(session, ids["season"], week_id, new_nine="back")
        match = _matches(session, week_id)[0]
        submit_card(session, week_id, _card(session, week_id, [match.a_golfer_id, match.b_golfer_id]),
                    user_id, TODAY, acknowledged=False)
        session.commit()
        with pytest.raises(ScheduleConflict, match="submitted scores"):
            confirm_nine(session, ids["season"], week_id, new_nine="back",
                         expected_fingerprint=preview.fingerprint)
        assert session.get(Week, week_id).nine == "front"
        assert session.scalar(select(func.count(HoleScore.id))) == 18
        no_change = preview_nine(session, ids["season"], week_id, new_nine="front")
        assert no_change.changes == ()
        confirm_nine(session, ids["season"], week_id, new_nine="front",
                     expected_fingerprint=no_change.fingerprint)


def test_rerotation_refuses_scored_downstream_week_atomically(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids, extra_weeks=(2,))
    scored_week_id = ids["weeks"][2]
    with Session(engine) as session:
        match = _matches(session, scored_week_id)[0]
        submit_card(session, scored_week_id,
                    _card(session, scored_week_id, [match.a_golfer_id, match.b_golfer_id]),
                    user_id, TODAY, acknowledged=False)
        session.commit()
        preview = preview_nine(session, ids["season"], ids["weeks"][0],
                               new_nine="back", rerotate=True)
        assert scored_week_id in {change.week_id for change in preview.changes}
        before = [(week.id, week.nine) for week in session.scalars(
            select(Week).where(Week.season_id == ids["season"]).order_by(Week.index)
        )]
        with pytest.raises(ScheduleConflict, match="submitted scores"):
            confirm_nine(session, ids["season"], ids["weeks"][0], new_nine="back",
                         rerotate=True, expected_fingerprint=preview.fingerprint)
        after = [(week.id, week.nine) for week in session.scalars(
            select(Week).where(Week.season_id == ids["season"]).order_by(Week.index)
        )]
        assert after == before
        assert session.scalar(select(func.count(HoleScore.id))) == 18


def test_scored_match_refuses_substitution_and_regeneration(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        match = _matches(session, week_id)[0]
        old_sides = (match.a_golfer_id, match.b_golfer_id)
        submit_card(session, week_id, _card(session, week_id, old_sides),
                    user_id, TODAY, acknowledged=False)
        session.commit()
        replacements = []
        for number in (1, 2):
            golfer = Golfer(first_name=f"Replacement{number}", last_name="Synthetic",
                            default_tee_set_id=ids["tee"], handicap_strokes=2,
                            handicap_source="self_reported", handicap_status="ok")
            session.add(golfer)
            session.flush()
            replacements.append(golfer.id)
            session.add(SeasonGolfer(season_id=ids["season"], golfer_id=golfer.id))
        session.commit()
        with pytest.raises(MatchupConflict, match="submitted scores"):
            substitute_player(session, ids["season"], match.id, side="a",
                              golfer_id=replacements[0], clock=matchup_clock)
        assert (session.get(PlayerMatch, match.id).a_golfer_id,
                session.get(PlayerMatch, match.id).b_golfer_id) == old_sides
        for position, golfer_id in enumerate(replacements, 1):
            member = session.scalar(select(TeamMember).where(
                TeamMember.team_id == ids["teams"][0], TeamMember.position == position
            ))
            member.golfer_id = golfer_id
        session.commit()
        with pytest.raises(MatchupConflict, match="submitted scores"):
            generate_week_matches(session, ids["season"], team_match_id=match.team_match_id,
                                  clock=matchup_clock)
        assert (session.get(PlayerMatch, match.id).a_golfer_id,
                session.get(PlayerMatch, match.id).b_golfer_id) == old_sides
        assert session.scalar(select(func.count(HoleScore.id))) == 18


def test_scored_season_refuses_course_change(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    with Session(engine) as session:
        week_id = ids["weeks"][0]
        match = _matches(session, week_id)[0]
        submit_card(session, week_id,
                    _card(session, week_id, [match.a_golfer_id, match.b_golfer_id]),
                    user_id, TODAY, acknowledged=False)
        session.commit()
        season = session.get(Season, ids["season"])
        old_course_id = season.course_id
        other = Course(name="Other Synthetic Course", total_holes=18)
        session.add(other)
        session.commit()
        with pytest.raises(SeasonValidationError) as caught:
            update_season(session, season.id, year=str(season.year), name_override="",
                          course_id=str(other.id), start_date=season.start_date.isoformat(),
                          end_date=season.end_date.isoformat(), play_weekday=str(season.play_weekday),
                          status=season.status, first_week_nine=season.first_week_nine)
        assert caught.value.errors["course_id"] == "Cannot change the course after scores have been submitted."
        session.rollback()
        assert session.get(Season, season.id).course_id == old_course_id
        assert session.scalar(select(func.count(HoleScore.id))) == 18


def test_scored_hole_refuses_nine_reassignment(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    with Session(engine) as session:
        week_id = ids["weeks"][0]
        match = _matches(session, week_id)[0]
        submit_card(session, week_id,
                    _card(session, week_id, [match.a_golfer_id, match.b_golfer_id]),
                    user_id, TODAY, acknowledged=False)
        session.commit()
        season = session.get(Season, ids["season"])
        holes = list(session.scalars(select(Hole).where(Hole.course_id == season.course_id).order_by(Hole.number)))
        front = holes[0]
        back = next(hole for hole in holes if hole.nine == "back" and hole.par == front.par)
        grid = []
        for hole in holes:
            grid.append({"number": hole.number, "nine": hole.nine, "par": hole.par,
                         "stroke_index_18": hole.stroke_index_18, "stroke_index_9": hole.stroke_index_9,
                         "yardages": {yardage.tee_set_id: yardage.yards for yardage in session.scalars(
                             select(HoleYardage).where(HoleYardage.hole_id == hole.id)
                         )}})
        for row in grid:
            if row["number"] == front.number:
                row["nine"] = "back"
                row["stroke_index_9"] = back.stroke_index_9
            elif row["number"] == back.number:
                row["nine"] = "front"
                row["stroke_index_9"] = front.stroke_index_9
        with pytest.raises(CourseValidationError) as caught:
            save_hole_grid(session, season.course_id, grid)
        assert caught.value.errors["holes"] == "Cannot change the nine of a hole with submitted scores."
        session.rollback()
        assert session.get(Hole, front.id).nine == "front"
        assert session.scalar(select(func.count(HoleScore.id))) == 18


def test_the_week_sheet_derives_totals(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    with Session(engine) as session:
        match = _matches(session, ids["weeks"][0])[0]
        numbers = [h.number for h in week_holes(session, ids["weeks"][0])]
        entries = [(match.a_golfer_id, dict.fromkeys(numbers, 5)),
                   (match.b_golfer_id, {n: ("4" if n < 5 else 6) for n in numbers})]
        submit_card(session, ids["weeks"][0], entries, user_id, TODAY, acknowledged=False)
        totals = {g.golfer_id: g.total for g in week_sheet(session, ids["weeks"][0])[0].golfers}
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
        assert _refusal(session, ids["weeks"][1], good, user_id) == "no_matchup"
        assert _refusal(session, 999999, good, user_id) == "not_found"


def test_a_card_only_adds_and_never_changes_a_saved_score(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        m1, m2 = _matches(session, week_id)[:2]
        first = _card(session, week_id, [m1.a_golfer_id, m1.b_golfer_id], value=3)
        submit_card(session, week_id, first, user_id, TODAY, acknowledged=False)
        session.commit()
        before = [(r.id, r.golfer_id, r.strokes) for r in session.scalars(select(HoleScore).order_by(HoleScore.id))]

        # The same card again: everyone on it is already saved.
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, _card(session, week_id, [m1.a_golfer_id, m1.b_golfer_id], value=6),
                        user_id, TODAY, acknowledged=False)
        assert caught.value.code == "nothing_left"
        assert set(caught.value.golfer_ids) == {m1.a_golfer_id, m1.b_golfer_id}
        session.rollback()

        # A card sharing one golfer with the saved one is refused until it names who is left off.
        overlap = _card(session, week_id, [m1.a_golfer_id, m2.a_golfer_id, m2.b_golfer_id], value=6)
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, overlap, user_id, TODAY, acknowledged=True)
        assert (caught.value.code, caught.value.golfer_ids) == ("left_off", (m1.a_golfer_id,))
        session.rollback()
        # Naming the wrong golfers, or more than are blocked, is refused again.
        for stale in ({m1.b_golfer_id}, {m1.a_golfer_id, m2.a_golfer_id}):
            with pytest.raises(ScoreEntryError) as caught:
                submit_card(session, week_id, overlap, user_id, TODAY, acknowledged=True, left_off=frozenset(stale))
            assert caught.value.code == "left_off"
            session.rollback()
        assert [(r.id, r.golfer_id, r.strokes)
                for r in session.scalars(select(HoleScore).order_by(HoleScore.id))] == before

        # Naming exactly the blocked golfer writes the rest and leaves the saved scores alone.
        result = submit_card(session, week_id, overlap, user_id, TODAY, acknowledged=True,
                             left_off=frozenset({m1.a_golfer_id}))
        session.commit()
        assert result.left_off == (m1.a_golfer_id,) and result.rows_written == 18
        assert set(result.golfer_ids) == {m2.a_golfer_id, m2.b_golfer_id}
        after = [(r.id, r.golfer_id, r.strokes) for r in session.scalars(select(HoleScore).order_by(HoleScore.id))]
        assert after[:len(before)] == before and len(after) == 36

        # A left-off token when nobody is blocked is refused too.
        m3 = _matches(session, week_id)[2]
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, _card(session, week_id, [m3.a_golfer_id, m3.b_golfer_id]), user_id,
                        TODAY, acknowledged=False, left_off=frozenset({m1.a_golfer_id}))
        assert (caught.value.code, caught.value.golfer_ids) == ("left_off", ())


def test_a_golfer_in_a_locked_match_is_left_off_a_card(matchup_db):
    engine, ids = matchup_db
    user_id = _setup(engine, ids)
    week_id = ids["weeks"][0]
    with Session(engine) as session:
        m1, m2 = _matches(session, week_id)[:2]
        scores_module.lock_match(session, week_id, m1.id, Actor(user_id, True), base_revision=0, now=NOW)
        session.commit()
        card = _card(session, week_id, [m1.a_golfer_id, m1.b_golfer_id, m2.a_golfer_id, m2.b_golfer_id])
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, card, user_id, TODAY, acknowledged=False)
        assert (caught.value.code, set(caught.value.golfer_ids)) == ("left_off", {m1.a_golfer_id, m1.b_golfer_id})
        session.rollback()
        submit_card(session, week_id, card, user_id, TODAY, acknowledged=False,
                    left_off=frozenset({m1.a_golfer_id, m1.b_golfer_id}))
        session.commit()
        assert {r.player_match_id for r in session.scalars(select(HoleScore))} == {m2.id}


def test_unique_key_violation_becomes_conflict_without_poisoning(matchup_db, monkeypatch):
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
        monkeypatch.setattr(scores_module, "blocked_golfers", lambda *args: frozenset())
        with pytest.raises(ScoreEntryError) as caught:
            submit_card(session, week_id, _card(session, week_id, [m1.a_golfer_id, m1.b_golfer_id]), user_id,
                        TODAY, acknowledged=False)
        assert caught.value.code == "conflict"
        # Caller's transaction is still usable and holds its earlier row and no state row.
        assert session.scalar(select(func.count(HoleScore.id))) == 1
        assert session.scalar(select(func.count(MatchScoreState.player_match_id))) == 0


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
                assert exc.code in {"nothing_left", "conflict"}
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count(True) == 1
    assert _count(engine) == 18
