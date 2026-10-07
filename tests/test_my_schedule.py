"""Verified player schedule projections, including makeup and substitution state."""

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import clock, seed_fixture

from golf_league.models import (
    Golfer,
    PlayerMatch,
    RosterImportBatch,
    RosterImportRow,
    Season,
    User,
    Week,
)
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.matchups import (
    create_team_match,
    generate_player_matches,
    substitute_player,
)


def _seed(client: TestClient, *, golfer_id=None, admin=False, self_match=False):
    with Session(client.app.state.engine) as session:
        ids = seed_fixture(session)
        season = session.get(Season, ids["season"])
        season.status = "active"
        session.add(Week(season_id=season.id, index=4, play_date=date(2026, 9, 17),
                         nine="back", week_type="match", status="scheduled"))
        user = User(
            username="viewer@example.test", email="viewer@example.test",
            display_name="Viewer", password_hash=hash_password("synthetic-password"),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=admin,
            golfer_id=golfer_id if golfer_id is not None else ids["golfers"][0],
        )
        session.add(user)
        pairing, _ = create_team_match(
            session, ids["weeks"][0], ids["teams"][0],
            ids["teams"][0] if self_match else ids["teams"][1], clock=clock,
        )
        generate_player_matches(session, pairing.id, clock=clock)
        session.commit()
        client.cookies.set(
            "session", create_session_cookie(user.id, user.session_version,
                                               client.app.state.settings.session_secret),
        )
        return ids


def test_schedule_shows_match_snapshot_and_all_week_states_without_private_fields(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        session.get(Week, ids["weeks"][1]).status = "cancelled"
        rain = session.get(Week, ids["weeks"][2])
        rain.week_type = "rain_date"
        rain.nine = None
        rain.makeup_for_week_id = ids["weeks"][1]
        private_values = ["viewer@example.test", "private-notes-marker", "raw-import-marker"]
        for index, golfer in enumerate(session.scalars(select(Golfer))):
            golfer.email = f"private-golfer-{index}@example.test"
            golfer.phone = f"555-010-{index:04d}"
            golfer.notes = "private-notes-marker"
            private_values.extend((golfer.email, golfer.phone))
        user = session.scalar(select(User))
        batch = RosterImportBatch(
            created_by_user_id=user.id, source_display_name="synthetic.csv",
            expires_at=datetime(2027, 1, 1),
        )
        session.add(batch)
        session.flush()
        session.add(RosterImportRow(
            batch_id=batch.id, position=1, source_role="player",
            source_file="synthetic.csv", source_row=1, raw_line="raw-import-marker",
        ))
        session.commit()
    response = client.get("/my/schedule")
    assert response.status_code == 200, response.text
    assert "My Schedule" in response.text and "A1 Example" in response.text
    assert "B1 Example" in response.text and "0" in response.text
    assert "Cancelled" in response.text and "Reserved rain date for" in response.text
    assert "awaiting pairing" in response.text
    assert f"/weeks/{ids['weeks'][0]}?season_id={ids['season']}" in response.text
    for private in private_values:
        assert private not in response.text


def test_schedule_auth_and_identity_come_from_verified_session(client):
    assert client.get("/my/schedule").status_code == 401
    _seed(client)
    with Session(client.app.state.engine) as session:
        user = session.scalar(select(User))
        user.email_verified_at = None
        session.commit()
    assert client.get("/my/schedule").status_code == 403


def test_played_week_retains_matchup_without_opponent_contact_link(client):
    ids = _seed(client)
    contact_url = f"/weeks/{ids['weeks'][0]}/opponents/{ids['golfers'][4]}"
    assert f'href="{contact_url}"' in client.get("/my/schedule").text
    with Session(client.app.state.engine) as session:
        session.get(Week, ids["weeks"][0]).status = "played"
        session.commit()
    page = client.get("/my/schedule")
    assert "B1 Example" in page.text
    assert f'href="{contact_url}"' not in page.text


def test_selected_season_is_validated_and_reserved_makeup_is_distinguished(client):
    ids = _seed(client)
    assert client.get("/my/schedule?season_id=99999").status_code == 404
    assert client.get("/my/schedule?season_id=not-a-number").status_code == 422
    with Session(client.app.state.engine) as session:
        weeks = list(session.scalars(select(Week).where(Week.season_id == ids["season"]).order_by(Week.index)))
        weeks[1].status = "cancelled"
        weeks[2].week_type = "match"
        weeks[2].makeup_for_week_id = weeks[1].id
        session.commit()
    page = client.get("/my/schedule")
    assert "Makeup for" in page.text and "Week 2" in page.text
    assert "Makeup for" in page.text and "Week 2" in page.text


def test_substitute_without_team_slot_sees_actual_matchup_not_old_assignment(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        row = session.scalar(select(PlayerMatch).order_by(PlayerMatch.id))
        sub = Golfer(first_name="Taylor", last_name="Substitute", handicap_strokes=0,
                     handicap_source="self_reported", handicap_status="ok")
        session.add(sub)
        session.flush()
        user = session.scalar(select(User))
        user.golfer_id = sub.id
        session.commit()
        substitute_player(session, ids["season"], row.id, side="a", golfer_id=sub.id, clock=clock)
    page = client.get("/my/schedule")
    assert "Taylor Substitute (sub)" in page.text and "B1 Example" in page.text
    assert "A1 Example" not in page.text
