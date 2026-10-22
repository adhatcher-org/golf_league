from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import clock, seed_fixture
from test_substitutions import matchup_db as _matchup_db
from test_substitutions import setup_sub

from golf_league.models import MatchScoreState, PlayerMatch, SubstituteRequest, User
from golf_league.routers.substitute_requests import _send_notice
from golf_league.services.auth import hash_password
from golf_league.services.email import FakeEmailSender
from golf_league.services.matchups import MatchupValidationError
from golf_league.services.substitute_requests import (
    RequestConflict,
    RequestNotFound,
    create_request,
    respond,
)

request_db = _matchup_db


def _setup(session, ids):
    ids, _, matches, substitute_id = setup_sub(session, ids)
    admin = User(username="admin@example.test", email="admin@example.test", display_name="Admin",
                 password_hash=hash_password("pw-test"),
                 email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=True)
    session.add(admin)
    session.commit()
    return ids, matches, substitute_id


def test_player_request_and_admin_fulfillment_is_single_use(request_db):
    with Session(request_db[0]) as session:
        ids, matches, sub_id = _setup(session, request_db[1])
        original = session.get(PlayerMatch, matches[0]).a_golfer_id
        row, token, recipients = create_request(session, week_id=ids["weeks"][0],
            player_match_id=matches[0], golfer_id=original, clock=clock)
        assert recipients == ["admin@example.test"]
        assert token != row.token_digest
        assert session.scalar(select(SubstituteRequest).where(SubstituteRequest.id == row.id)).status == "pending"
        with pytest.raises(RequestConflict):
            create_request(session, week_id=ids["weeks"][0], player_match_id=matches[0],
                           golfer_id=original, clock=clock)
        with pytest.raises(RequestNotFound):
            respond(session, request_id=row.id, raw_token="bad", action="fulfill", golfer_id=sub_id, clock=clock)
        respond(session, request_id=row.id, raw_token=token, action="fulfill", golfer_id=sub_id, clock=clock)
        assert session.get(PlayerMatch, matches[0]).a_golfer_id == sub_id
        assert session.get(SubstituteRequest, row.id).status == "fulfilled"
        with pytest.raises(RequestNotFound):
            respond(session, request_id=row.id, raw_token=token, action="decline", golfer_id=None, clock=clock)


def test_request_rejects_other_golfer_and_locked_match(request_db):
    with Session(request_db[0]) as session:
        ids, matches, _ = _setup(session, request_db[1])
        original = session.get(PlayerMatch, matches[0]).a_golfer_id
        with pytest.raises(RequestNotFound):
            create_request(session, week_id=ids["weeks"][0], player_match_id=matches[0],
                           golfer_id=None, clock=clock)
        with pytest.raises(RequestNotFound):
            create_request(session, week_id=ids["weeks"][0], player_match_id=matches[0],
                           golfer_id=ids["golfers"][7], clock=clock)
        session.add(MatchScoreState(player_match_id=matches[0], revision=1,
            locked_at=clock(), locked_by_user_id=session.scalar(select(User.id))))
        session.commit()
        with pytest.raises(RequestConflict):
            create_request(session, week_id=ids["weeks"][0], player_match_id=matches[0],
                           golfer_id=original, clock=clock)


def test_response_rolls_back_on_ineligible_substitute(request_db):
    with Session(request_db[0]) as session:
        ids, matches, _ = _setup(session, request_db[1])
        original = session.get(PlayerMatch, matches[0]).a_golfer_id
        row, token, _ = create_request(session, week_id=ids["weeks"][0],
            player_match_id=matches[0], golfer_id=original, clock=clock)
        with pytest.raises(MatchupValidationError):
            respond(session, request_id=row.id, raw_token=token, action="fulfill",
                    golfer_id=ids["golfers"][1], clock=clock)
        assert session.get(SubstituteRequest, row.id).status == "pending"
        assert session.get(PlayerMatch, matches[0]).a_golfer_id == original


def test_email_failure_removes_undelivered_request(request_db):
    with Session(request_db[0]) as session:
        ids, matches, _ = _setup(session, request_db[1])
        original = session.get(PlayerMatch, matches[0]).a_golfer_id
        row, token, recipients = create_request(session, week_id=ids["weeks"][0],
            player_match_id=matches[0], golfer_id=original, clock=clock)
        request_id = row.id

    class BrokenSender:
        def send(self, **_kwargs):
            raise OSError("simulated mail failure")

    app = SimpleNamespace(state=SimpleNamespace(engine=request_db[0], email_sender=BrokenSender(),
                                                settings=SimpleNamespace(external_base_url="https://example.test")))
    _send_notice(app, request_id, token, recipients, ids["weeks"][0], matches[0])
    with Session(request_db[0]) as session:
        assert session.get(SubstituteRequest, request_id) is None


def test_notice_contains_response_token_only_in_email(request_db):
    with Session(request_db[0]) as session:
        ids, matches, _ = _setup(session, request_db[1])
        original = session.get(PlayerMatch, matches[0]).a_golfer_id
        row, token, recipients = create_request(session, week_id=ids["weeks"][0],
            player_match_id=matches[0], golfer_id=original, clock=clock)
        request_id = row.id
    sender = FakeEmailSender()
    app = SimpleNamespace(state=SimpleNamespace(engine=request_db[0], email_sender=sender,
                                                settings=SimpleNamespace(external_base_url="https://example.test")))
    _send_notice(app, request_id, token, recipients, ids["weeks"][0], matches[0])
    assert len(sender.sent) == 1
    assert token in sender.sent[0]["body"]
    assert token not in sender.sent[0]["subject"]


def test_player_route_reports_failed_delivery_and_allows_retry(empty_client):
    from conftest import _extract_csrf

    client = empty_client
    with Session(client.app.state.engine) as session:
        ids = seed_fixture(session)
        ids, matches, _ = _setup(session, ids)
        golfer_id = session.get(PlayerMatch, matches[0]).a_golfer_id
        session.add(User(username="player@example.test", email="player@example.test",
                         display_name="Player", password_hash=hash_password("pw-test"),
                         email_verified_at=datetime.now(UTC).replace(tzinfo=None),
                         is_admin=False, golfer_id=golfer_id))
        session.commit()

    class BrokenSender:
        def send(self, **_kwargs):
            raise OSError("simulated mail failure")

    client.app.state.email_sender = BrokenSender()
    login_csrf = _extract_csrf(client.get("/login").text)
    assert client.post("/login", data={"email": "player@example.test", "password": "pw-test",
                                       "csrf_token": login_csrf}, follow_redirects=False).status_code == 303
    path = f"/weeks/{ids['weeks'][0]}/matches/{matches[0]}/substitute-request"
    csrf = _extract_csrf(client.get(path).text)
    failed = client.post(path, data={"csrf_token": csrf}, follow_redirects=False)
    assert failed.status_code == 503
    assert "could not be delivered" in failed.text
    assert "sent=1" not in failed.headers.get("location", "")
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(SubstituteRequest)) is None
    sender = FakeEmailSender()
    client.app.state.email_sender = sender
    retried = client.post(path, data={"csrf_token": csrf}, follow_redirects=False)
    assert retried.status_code == 303
    assert len(sender.sent) == 1


def test_player_and_admin_routes_enforce_access_and_csrf(empty_client):
    from conftest import _extract_csrf

    client = empty_client
    with Session(client.app.state.engine) as session:
        ids = seed_fixture(session)
        ids, matches, substitute_id = _setup(session, ids)
        golfer_id = session.get(PlayerMatch, matches[0]).a_golfer_id
        player = User(username="player@example.test", email="player@example.test",
                      display_name="Player", password_hash=hash_password("pw-test"),
                      email_verified_at=datetime.now(UTC).replace(tzinfo=None),
                      is_admin=False, golfer_id=golfer_id)
        session.add(player)
        session.commit()
    path = f"/weeks/{ids['weeks'][0]}/matches/{matches[0]}/substitute-request"
    assert client.get(path).status_code == 401
    login_csrf = _extract_csrf(client.get("/login").text)
    assert client.post("/login", data={"email": "player@example.test", "password": "pw-test",
                                       "csrf_token": login_csrf}, follow_redirects=False).status_code == 303
    week_page = client.get(f"/weeks/{ids['weeks'][0]}")
    assert week_page.status_code == 200
    assert f'href="{path}"' in week_page.text
    page = client.get(path)
    assert page.status_code == 200
    assert client.post(path, data={"csrf_token": "bad"}, follow_redirects=False).status_code == 403
    assert client.post(path, data={"csrf_token": _extract_csrf(page.text)},
                       follow_redirects=False).status_code == 303
    assert len(client.app.state.email_sender.sent) == 1
    token = client.app.state.email_sender.sent[0]["body"].split("token: ", 1)[1].strip()
    assert token not in page.text
    assert client.post(path, data={"csrf_token": _extract_csrf(page.text)},
                       follow_redirects=False).status_code == 409
    assert client.post("/logout", data={"csrf_token": _extract_csrf(page.text)},
                       follow_redirects=False).status_code == 303
    login_csrf = _extract_csrf(client.get("/login").text)
    assert client.post("/login", data={"email": "admin@example.test", "password": "pw-test",
                                       "csrf_token": login_csrf}, follow_redirects=False).status_code == 303
    admin_page = client.get("/admin/substitute-requests")
    assert admin_page.status_code == 200
    assert token not in admin_page.text
    response_path = "/admin/substitute-requests/1/respond"
    csrf = _extract_csrf(admin_page.text)
    assert client.post(response_path, data={"action": "fulfill", "response_token": "bad",
                                            "golfer_id": substitute_id, "csrf_token": csrf},
                       follow_redirects=False).status_code == 404
    assert client.post(response_path, data={"action": "fulfill", "response_token": token,
                                            "golfer_id": substitute_id, "csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    assert client.post(response_path, data={"action": "decline", "response_token": token,
                                            "csrf_token": csrf}, follow_redirects=False).status_code == 404
