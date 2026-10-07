"""M6 aggregate gate: authenticated player journeys over generated matchups."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import clock
from test_my_schedule import _seed

from golf_league.models import Golfer, PlayerMatch, User, Week
from golf_league.services.matchups import substitute_player
from golf_league.services.schedule import confirm_rain_date, preview_rain_date


@pytest.mark.parametrize("persona", ["regular", "substitute", "self_match"])
def test_three_player_personas_use_actual_generated_matchups(client, persona):
    ids = _seed(client, self_match=persona == "self_match")
    if persona == "substitute":
        with Session(client.app.state.engine) as session:
            substitute = Golfer(first_name="Taylor", last_name="Substitute",
                                handicap_strokes=0, handicap_source="self_reported",
                                handicap_status="ok")
            session.add(substitute)
            session.flush()
            session.scalar(select(User)).golfer_id = substitute.id
            row = session.scalar(select(PlayerMatch).order_by(PlayerMatch.id))
            session.commit()
            substitute_player(session, ids["season"], row.id, side="a",
                              golfer_id=substitute.id, clock=clock)
    page = client.get("/my/schedule")
    assert page.status_code == 200
    assert "Aug 27, 2026" in page.text and "Front" in page.text
    if persona == "substitute":
        assert "Taylor Substitute (sub) (0)" in page.text
        assert "B1 Example" in page.text and "A1 Example" not in page.text
    elif persona == "self_match":
        assert "A1 Example" in page.text and "A2 Example" in page.text
        assert "Self-match: both sides play against their own handicap." in page.text
        assert "against own handicap" in page.text
        assert "B1 Example" not in page.text
    else:
        assert "A1 Example (-1)" in page.text and "B1 Example" in page.text
    assert "Edit week" not in page.text
    assert "Standings" not in page.text and "Winner" not in page.text


def test_unused_substitute_and_unlinked_user_have_graceful_schedule(client):
    _seed(client)
    with Session(client.app.state.engine) as session:
        unused = Golfer(first_name="Unused", last_name="Substitute",
                        handicap_source="self_reported", handicap_status="missing")
        session.add(unused)
        session.flush()
        session.scalar(select(User)).golfer_id = unused.id
        session.commit()
    unused_page = client.get("/my/schedule")
    assert unused_page.status_code == 200
    assert "No participation" in unused_page.text
    assert "B1 Example" not in unused_page.text
    with Session(client.app.state.engine) as session:
        session.scalar(select(User)).golfer_id = None
        session.commit()
    unlinked_page = client.get("/my/schedule")
    assert unlinked_page.status_code == 200
    assert "No participation" in unlinked_page.text
    assert "B1 Example" not in unlinked_page.text


def test_cancelled_origin_and_activated_makeup_keep_navigation(client):
    ids = _seed(client)
    source, target = ids["weeks"][0], ids["weeks"][2]
    with Session(client.app.state.engine) as session:
        rain = session.get(Week, target)
        rain.week_type, rain.nine = "rain_date", None
        session.commit()
        preview = preview_rain_date(session, ids["season"], source, target)
        confirm_rain_date(session, ids["season"], source, target,
                          expected_fingerprint=preview.fingerprint)
    schedule = client.get("/my/schedule")
    assert schedule.status_code == 200
    assert "Cancelled" in schedule.text and "Makeup for" in schedule.text
    assert f'href="/weeks/{target}?season_id={ids["season"]}"' in schedule.text
    origin_page = client.get(f"/weeks/{source}")
    makeup_page = client.get(f"/weeks/{target}")
    assert origin_page.status_code == makeup_page.status_code == 200
    assert "Cancelled" in origin_page.text and "Makeup:" in origin_page.text
    assert "Makeup for" in makeup_page.text and "B1 Example" in makeup_page.text
    assert f'href="/my/schedule?season_id={ids["season"]}"' in makeup_page.text


def test_week_and_home_show_all_league_matches_without_initial_contacts(client, monkeypatch):
    ids = _seed(client)
    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 8, 27)
    monkeypatch.setattr("golf_league.routers.home.date", FixedDate)
    with Session(client.app.state.engine) as session:
        opponent = session.get(Golfer, ids["golfers"][4])
        opponent.email, opponent.phone = "m6-opponent@example.test", "+1-555-010-0600"
        opponent.notes = "M6_PRIVATE_IMPORT_NOTE"
        session.commit()
    for path in ("/", "/my/schedule", f"/weeks/{ids['weeks'][0]}"):
        page = client.get(path)
        assert page.status_code == 200
        for value in ("m6-opponent@example.test", "+1-555-010-0600", "M6_PRIVATE_IMPORT_NOTE"):
            assert value not in page.text
        assert "Edit week" not in page.text
    week = client.get(f"/weeks/{ids['weeks'][0]}")
    home = client.get("/")
    assert "A4 Example" in week.text and "B4 Example" in week.text
    assert "A4 Example" in home.text and "B4 Example" in home.text
    assert f'href="/weeks/{ids["weeks"][0]}"' in home.text
    assert 'href="/my/schedule"' in home.text
    contact = client.get(f"/weeks/{ids['weeks'][0]}/opponents/{ids['golfers'][4]}",
                         headers={"Accept": "application/json"})
    assert contact.status_code == 200
    assert contact.json()["email"] == "m6-opponent@example.test"
    assert contact.json()["phone"] == "+1-555-010-0600"
    assert contact.headers["cache-control"] == "no-store"
    assert client.get(f"/weeks/{ids['weeks'][0]}/opponents/{ids['golfers'][5]}").status_code == 404


def test_authentication_and_verification_guard_player_journeys(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        session.scalar(select(User)).email_verified_at = None
        session.commit()
    for path in ("/my/schedule", f"/weeks/{ids['weeks'][0]}"):
        assert client.get(path).status_code == 403
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303 and home.headers["location"] == "/login?next=/"
    client.cookies.clear()
    assert client.get("/my/schedule").status_code == 401
    assert client.get(f"/weeks/{ids['weeks'][0]}").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
