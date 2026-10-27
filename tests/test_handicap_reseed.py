"""Handicap re-seed: pure domain `reseed_plan` + service + router tests."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import matchup_db as _matchup_db
from test_matchup_generation import seed_fixture

from golf_league.domain.reseed import ReseedCandidate, ReseedChange, reseed_plan
from golf_league.domain.scorecard import Actor
from golf_league.models import (
    Golfer,
    PlayerMatch,
    Season,
    SeasonParticipant,
    TeamMatch,
    Week,
    WeekHandicap,
)
from golf_league.security import generate_csrf_token
from golf_league.services.handicaps import (
    HandicapReseedError,
    ReseedPreview,
    ensure_week_handicap,
    reseed_preview,
    reseed_week_handicaps,
)
from golf_league.services.matchups import create_team_match, generate_player_matches

matchup_db = _matchup_db
NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


def _token(client) -> str:
    """The CSRF token bound to the client's session cookie."""
    return generate_csrf_token(client.cookies.get("session") or "")


def _candidate(golfer_id, name, current_strokes, effective_handicap, manually_adjusted=False):
    return ReseedCandidate(
        golfer_id=golfer_id,
        golfer_name=name,
        current_strokes=current_strokes,
        effective_handicap=effective_handicap,
        manually_adjusted=manually_adjusted,
    )


# --- domain unit tests -------------------------------------------------------


def test_reseed_plan_all_changes():
    plan = reseed_plan([
        _candidate(1, "A", 10, 12),
        _candidate(2, "B", 15, 14),
    ])
    assert plan.changes == (
        ReseedChange(golfer_id=1, golfer_name="A", current_strokes=10, new_strokes=12),
        ReseedChange(golfer_id=2, golfer_name="B", current_strokes=15, new_strokes=14),
    )
    assert plan.unchanged == ()
    assert plan.blocked_no_seed == ()
    assert plan.manually_adjusted == ()
    assert plan.excluded_no_row == ()


def test_reseed_plan_categorises_all_five_groups():
    plan = reseed_plan([
        _candidate(1, "Will Change", 10, 12),          # changes
        _candidate(2, "Unchanged", 10, 10),             # unchanged
        _candidate(3, "Blocked No Seed", 10, None),     # blocked_no_seed
        _candidate(4, "Manually Adjusted", 10, 12, True),  # manually_adjusted
        _candidate(5, "Excluded No Row", None, 12),     # excluded_no_row
    ])
    assert plan.changes == (ReseedChange(golfer_id=1, golfer_name="Will Change", current_strokes=10, new_strokes=12),)
    assert plan.unchanged == (2,)
    assert plan.blocked_no_seed == (3,)
    assert plan.manually_adjusted == (4,)
    assert plan.excluded_no_row == (5,)


def test_reseed_plan_negative_handicap_is_valid():
    """Plus handicaps (negative) are valid and never coerced."""
    plan = reseed_plan([
        _candidate(1, "Plus", 3, -2),
    ])
    assert plan.changes == (ReseedChange(golfer_id=1, golfer_name="Plus", current_strokes=3, new_strokes=-2),)


def test_reseed_plan_zero_is_scratch_not_missing():
    """0 strokes is a valid scratch value, not missing."""
    plan = reseed_plan([
        _candidate(1, "Scratch", 0, 5),
        _candidate(2, "Already Scratch", 0, 0),
    ])
    assert plan.changes == (ReseedChange(golfer_id=1, golfer_name="Scratch", current_strokes=0, new_strokes=5),)
    assert plan.unchanged == (2,)


def test_reseed_plan_null_strokes_excluded():
    """NULL current_strokes means no WeekHandicap row → excluded."""
    plan = reseed_plan([
        _candidate(1, "No Row", None, 10),
        _candidate(2, "No Row 2", None, None),
    ])
    assert plan.changes == ()
    assert plan.excluded_no_row == (1, 2)


def test_reseed_plan_idempotent():
    """Running with no changes produces an empty changes tuple."""
    plan = reseed_plan([
        _candidate(1, "Same", 10, 10),
        _candidate(2, "Blocked", 10, None),
    ])
    assert plan.changes == ()
    assert plan.unchanged == (1,)
    assert plan.blocked_no_seed == (2,)


def test_reseed_plan_empty_input():
    plan = reseed_plan([])
    assert plan.changes == ()
    assert plan.unchanged == ()
    assert plan.blocked_no_seed == ()
    assert plan.manually_adjusted == ()
    assert plan.excluded_no_row == ()


# --- service integration tests ----------------------------------------------


def _seed_with_handicaps(session, ids, week_index=0):
    """Generate player matches and WeekHandicap rows for a week."""
    week_id = ids["weeks"][week_index]
    # Create a team match first (needed for PlayerMatch slots).
    pairing, _ = create_team_match(
        session, week_id, ids["teams"][0], ids["teams"][1], clock=lambda: NOW
    )
    generate_player_matches(session, pairing.id, clock=lambda: NOW)
    session.commit()

    # Create WeekHandicap rows for every golfer in the week.
    for golfer_id in ids["golfers"]:
        ensure_week_handicap(session, week_id, golfer_id, clock=lambda: NOW)
    session.commit()
    return week_id


def test_reseed_preview_shows_all_categories(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        week_id = _seed_with_handicaps(session, ids, week_index=0)

        # Change golfer 0's seed handicap so their strokes differ.
        session.add(SeasonParticipant(
            season_id=ids["season"], golfer_id=ids["golfers"][0],
            tee_set_id=ids["tee"], seed_handicap_strokes=-4,
        ))
        session.commit()

        preview = reseed_preview(session, week_id)
        assert isinstance(preview, ReseedPreview)
        assert preview.week_id == week_id
        assert preview.nine == "front"

        # Golfer 0: strokes= -1 (from seed_fixture), effective=-4 → change
        changes_by_id = {c.golfer_id: c for c in preview.plan.changes}
        assert ids["golfers"][0] in changes_by_id
        assert changes_by_id[ids["golfers"][0]].new_strokes == -4

        # Other golfers: strokes match effective → unchanged
        assert ids["golfers"][1] in preview.plan.unchanged
        assert ids["golfers"][2] in preview.plan.unchanged
        assert ids["golfers"][3] in preview.plan.unchanged


def test_reseed_week_handicaps_overwrites_only_changes(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        week_id = _seed_with_handicaps(session, ids, week_index=0)

        # Change golfer 0's seed.
        session.add(SeasonParticipant(
            season_id=ids["season"], golfer_id=ids["golfers"][0],
            tee_set_id=ids["tee"], seed_handicap_strokes=-4,
        ))
        session.commit()

        before = reseed_preview(session, week_id)
        assert len(before.plan.changes) == 1

        actor = Actor(user_id=1, is_admin=True)
        count = reseed_week_handicaps(session, week_id, actor, NOW)
        assert count == 1
        session.commit()

        # Verify the change was persisted.
        row = session.scalar(select(WeekHandicap).where(
            WeekHandicap.week_id == week_id, WeekHandicap.golfer_id == ids["golfers"][0],
        ))
        assert row.strokes == -4
        assert row.source == "seeded"
        assert row.computed_at == NOW

        # Re-seed again: nothing should change (idempotent).
        after = reseed_preview(session, week_id)
        assert after.plan.changes == ()
        count2 = reseed_week_handicaps(session, week_id, actor, NOW)
        assert count2 == 0


def test_reseed_skips_manually_adjusted(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        week_id = _seed_with_handicaps(session, ids, week_index=0)

        # Mark golfer 1's match slot as manually adjusted.
        team_match = session.scalar(select(TeamMatch).where(TeamMatch.week_id == week_id))
        slot_row = session.scalar(select(PlayerMatch).where(
            PlayerMatch.team_match_id == team_match.id,
            (PlayerMatch.a_golfer_id == ids["golfers"][1]) | (PlayerMatch.b_golfer_id == ids["golfers"][1]),
        ))
        slot_row.manually_adjusted = True
        session.commit()

        # Change golfer 1's seed so it would differ.
        session.add(SeasonParticipant(
            season_id=ids["season"], golfer_id=ids["golfers"][1],
            tee_set_id=ids["tee"], seed_handicap_strokes=99,
        ))
        session.commit()

        preview = reseed_preview(session, week_id)
        assert ids["golfers"][1] in preview.plan.manually_adjusted
        assert ids["golfers"][1] not in {c.golfer_id for c in preview.plan.changes}

        actor = Actor(user_id=1, is_admin=True)
        count = reseed_week_handicaps(session, week_id, actor, NOW)
        assert count == 0  # golfer 1 skipped, golfer 0 unchanged

        # Verify golfer 1's row was NOT updated.
        row = session.scalar(select(WeekHandicap).where(
            WeekHandicap.week_id == week_id, WeekHandicap.golfer_id == ids["golfers"][1],
        ))
        assert row.strokes != 99


def test_reseed_blocks_no_effective_seed(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        week_id = _seed_with_handicaps(session, ids, week_index=0)

        # Remove golfer 2's default tee set AND label → no effective handicap.
        golfer = session.get(Golfer, ids["golfers"][2])
        golfer.default_tee_set_id = None
        golfer.default_tee_label = "No Such Tee"
        session.commit()

        preview = reseed_preview(session, week_id)
        assert ids["golfers"][2] in preview.plan.blocked_no_seed
        assert ids["golfers"][2] not in {c.golfer_id for c in preview.plan.changes}


def test_reseed_preview_unknown_week(matchup_db):
    engine, _ = matchup_db
    with Session(engine) as session:
        with pytest.raises(HandicapReseedError) as exc:
            reseed_preview(session, 999999)
        assert exc.value.code == "week_not_found"


def test_reseed_preview_rain_date_week(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        from golf_league.models import Week
        # Create a rain_date week (no nine).
        week = Week(
            season_id=ids["season"], index=99, play_date=date(2026, 12, 25),
            nine=None, week_type="rain_date", status="scheduled",
        )
        session.add(week)
        session.commit()
        with pytest.raises(HandicapReseedError) as exc:
            reseed_preview(session, week.id)
        assert exc.value.code == "not_schedulable"


# --- router integration tests ----------------------------------------------


@pytest.fixture
def reseed(admin_client):
    """A logged-in admin, week 1 with player matches and WeekHandicap rows."""
    client = admin_client
    with Session(client.app.state.engine) as session:
        ids = seed_fixture(session)
        session.get(Season, ids["season"]).status = "active"
        pairing, _ = create_team_match(
            session, ids["weeks"][0], ids["teams"][0], ids["teams"][1], clock=lambda: NOW
        )
        generate_player_matches(session, pairing.id, clock=lambda: NOW)
        for golfer_id in ids["golfers"]:
            ensure_week_handicap(session, ids["weeks"][0], golfer_id, clock=lambda: NOW)
        session.commit()
    return client, ids


def _reseed_path(week_id: int) -> str:
    return f"/admin/weeks/{week_id}/handicaps/reseed"


def test_get_reseed_preview_shows_changes(reseed):
    client, ids = reseed
    week_id = ids["weeks"][0]
    with Session(client.app.state.engine) as session:
        session.add(SeasonParticipant(
            season_id=ids["season"], golfer_id=ids["golfers"][0],
            tee_set_id=ids["tee"], seed_handicap_strokes=-4,
        ))
        session.commit()

    response = client.get(_reseed_path(week_id), follow_redirects=False)
    assert response.status_code == 200
    html = response.text
    assert 'name="csrf_token" value="' in html
    assert "Will change" in html


def test_get_reseed_unknown_week(reseed):
    client, _ = reseed
    response = client.get("/admin/weeks/999999/handicaps/reseed", follow_redirects=False)
    assert response.status_code == 404


def test_get_reseed_rain_date_week(reseed):
    client, ids = reseed
    with Session(client.app.state.engine) as session:
        week = Week(
            season_id=ids["season"], index=99, play_date=date(2026, 12, 25),
            nine=None, week_type="rain_date", status="scheduled",
        )
        session.add(week)
        session.commit()
        week_id = week.id

    response = client.get(_reseed_path(week_id), follow_redirects=False)
    assert response.status_code == 422


def test_post_reseed_without_csrf_is_refused(reseed):
    client, ids = reseed
    response = client.post(_reseed_path(ids["weeks"][0]), data={}, follow_redirects=False)
    assert response.status_code == 403


def test_post_reseed_bad_csrf_is_refused(reseed):
    client, ids = reseed
    response = client.post(
        _reseed_path(ids["weeks"][0]),
        data={"csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_post_reseed_confirms_changes(reseed):
    client, ids = reseed
    week_id = ids["weeks"][0]
    with Session(client.app.state.engine) as session:
        session.add(SeasonParticipant(
            season_id=ids["season"], golfer_id=ids["golfers"][0],
            tee_set_id=ids["tee"], seed_handicap_strokes=-4,
        ))
        session.commit()

    get_response = client.get(_reseed_path(week_id), follow_redirects=False)
    assert get_response.status_code == 200
    csrf_token = _token(client)

    response = client.post(_reseed_path(week_id),
                           data={"csrf_token": csrf_token},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "reseeded=1" in response.headers["location"]

    with Session(client.app.state.engine) as session:
        row = session.scalar(select(WeekHandicap).where(
            WeekHandicap.week_id == week_id,
            WeekHandicap.golfer_id == ids["golfers"][0],
        ))
        assert row.strokes == -4
        assert row.source == "seeded"


def test_post_reseed_idempotent(reseed):
    client, ids = reseed
    week_id = ids["weeks"][0]
    with Session(client.app.state.engine) as session:
        session.add(SeasonParticipant(
            season_id=ids["season"], golfer_id=ids["golfers"][0],
            tee_set_id=ids["tee"], seed_handicap_strokes=-4,
        ))
        session.commit()

    csrf_token = _token(client)

    response1 = client.post(_reseed_path(week_id),
                            data={"csrf_token": csrf_token},
                            follow_redirects=False)
    assert response1.status_code == 303
    assert "reseeded=1" in response1.headers["location"]

    response2 = client.post(_reseed_path(week_id),
                            data={"csrf_token": csrf_token},
                            follow_redirects=False)
    assert response2.status_code == 303
    assert "reseeded=0" in response2.headers["location"]


def test_post_reseed_skips_manually_adjusted(reseed):
    client, ids = reseed
    week_id = ids["weeks"][0]

    with Session(client.app.state.engine) as session:
        team_match = session.scalar(select(TeamMatch).where(TeamMatch.week_id == week_id))
        slot_row = session.scalar(select(PlayerMatch).where(
            PlayerMatch.team_match_id == team_match.id,
            (PlayerMatch.a_golfer_id == ids["golfers"][1]) | (PlayerMatch.b_golfer_id == ids["golfers"][1]),
        ))
        slot_row.manually_adjusted = True
        session.add(SeasonParticipant(
            season_id=ids["season"], golfer_id=ids["golfers"][1],
            tee_set_id=ids["tee"], seed_handicap_strokes=99,
        ))
        session.commit()

    csrf_token = _token(client)

    response = client.post(_reseed_path(week_id),
                           data={"csrf_token": csrf_token},
                           follow_redirects=False)
    assert response.status_code == 303

    with Session(client.app.state.engine) as session:
        row = session.scalar(select(WeekHandicap).where(
            WeekHandicap.week_id == week_id,
            WeekHandicap.golfer_id == ids["golfers"][1],
        ))
        assert row.strokes != 99
