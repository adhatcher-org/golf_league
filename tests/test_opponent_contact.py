"""Contact responses are scoped to the viewer's current direct opponent."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import clock
from test_my_schedule import _seed

from golf_league.models import Golfer, PlayerMatch, User, Week
from golf_league.services.matchups import substitute_player


@pytest.fixture(params=["text/html", "application/json"])
def accept(request):
    return {"Accept": request.param}


def _contact_fixture(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        opponent = session.get(Golfer, ids["golfers"][4])
        opponent.email = "opponent@example.test"
        opponent.phone = "+1-555-010-0200"
        opponent.notes = "private-opponent-notes"
        unrelated = session.get(Golfer, ids["golfers"][8])
        unrelated.email = "unrelated@example.test"
        session.commit()
    return ids


def _url(ids, *, week_id=None, golfer_id=None):
    return (f"/weeks/{week_id if week_id is not None else ids['weeks'][0]}"
            f"/opponents/{golfer_id if golfer_id is not None else ids['golfers'][4]}")


def test_contact_requires_verified_session(client, accept):
    ids = _contact_fixture(client)
    client.cookies.clear()
    assert client.get(_url(ids), headers=accept).status_code == 401
    # Restore the real signed session, then revoke verification.
    from golf_league.services.auth import create_session_cookie

    with Session(client.app.state.engine) as session:
        user = session.scalar(select(User))
        user.email_verified_at = None
        client.cookies.set("session", create_session_cookie(
            user.id, user.session_version, client.app.state.settings.session_secret,
        ))
        session.commit()
    assert client.get(_url(ids), headers=accept).status_code == 403


def test_direct_opponent_contact_and_html_communication_links(client, accept):
    ids = _contact_fixture(client)
    response = client.get(_url(ids), headers=accept)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "private-opponent-notes" not in response.text
    assert "unrelated@example.test" not in response.text
    if accept["Accept"] == "application/json":
        assert response.json() == {
            "name": "B1 Example", "email": "opponent@example.test",
            "phone": "+1-555-010-0200", "week_index": 1, "week_date": "Aug 27, 2026",
        }
    else:
        assert "B1 Example" in response.text
        for href in ("mailto:opponent@example.test", "tel:+1-555-010-0200", "sms:+1-555-010-0200"):
            assert f'href="{href}"' in response.text


@pytest.mark.parametrize("case", [
    "unrelated", "unknown_week", "unpaired_week", "other_slot", "unlinked", "self", "cancelled",
])
def test_contact_rejects_lookup_outside_direct_scheduled_match(client, accept, case):
    ids = _contact_fixture(client)
    week_id, golfer_id = ids["weeks"][0], ids["golfers"][4]
    with Session(client.app.state.engine) as session:
        if case == "unrelated":
            golfer_id = ids["golfers"][8]
        elif case == "unknown_week":
            week_id = 99999
        elif case == "unpaired_week":
            week_id = ids["weeks"][1]
        elif case == "other_slot":
            golfer_id = ids["golfers"][5]
        elif case == "unlinked":
            session.scalar(select(User)).golfer_id = None
        elif case == "self":
            golfer_id = ids["golfers"][0]
        elif case == "cancelled":
            session.get(Week, week_id).status = "cancelled"
        session.commit()
    response = client.get(_url(ids, week_id=week_id, golfer_id=golfer_id), headers=accept)
    assert response.status_code == 404
    assert "opponent@example.test" not in response.text
    assert "+1-555-010-0200" not in response.text


def test_substitution_revokes_replaced_player_and_grants_current_opponent(client, accept):
    ids = _contact_fixture(client)
    with Session(client.app.state.engine) as session:
        row = session.scalar(select(PlayerMatch).where(PlayerMatch.a_golfer_id == ids["golfers"][0]))
        replacement = Golfer(
            first_name="Taylor", last_name="Substitute", email="substitute@example.test",
            handicap_strokes=0, handicap_source="self_reported", handicap_status="ok",
        )
        session.add(replacement)
        session.commit()
        replacement_id = replacement.id
        substitute_player(session, ids["season"], row.id, side="a", golfer_id=replacement_id, clock=clock)
    assert client.get(_url(ids), headers=accept).status_code == 404
    with Session(client.app.state.engine) as session:
        session.scalar(select(User)).golfer_id = ids["golfers"][4]
        session.commit()
    assert client.get(_url(ids, golfer_id=ids["golfers"][0]), headers=accept).status_code == 404
    response = client.get(_url(ids, golfer_id=replacement_id), headers=accept)
    assert response.status_code == 200
    assert "substitute@example.test" in response.text


def test_makeup_generated_match_allows_opponent_contact(client, accept):
    ids = _contact_fixture(client)
    with Session(client.app.state.engine) as session:
        session.get(Week, ids["weeks"][0]).makeup_for_week_id = ids["weeks"][1]
        session.get(Week, ids["weeks"][1]).status = "cancelled"
        session.commit()
    assert client.get(_url(ids), headers=accept).status_code == 200
