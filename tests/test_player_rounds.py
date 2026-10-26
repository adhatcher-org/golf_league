"""Tests for player round history (GL-103)."""

import re
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from golf_league.models import User
from golf_league.services.auth import hash_password
from golf_league.services.player_rounds import get_player_rounds


def _make_user(session: Session, email: str, password: str, golfer_id: int | None) -> User:
    user = User(
        username=email,
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(password),
        email_verified_at=datetime(2024, 1, 1),
        golfer_id=golfer_id,
        is_admin=False,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _login(client: TestClient, email: str, password: str) -> None:
    login_page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]*)"', login_page.text)
    assert match, "no csrf_token field found"
    csrf_token = match.group(1)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303


class TestPlayerRoundsService:
    """Service layer tests for player round history."""

    def test_get_player_rounds_empty(self, session: Session) -> None:
        """Returns empty list when golfer has no rounds."""
        rounds = get_player_rounds(session, 999)
        assert rounds == ()

    def test_get_player_rounds_negative_golfer_id(self, session: Session) -> None:
        """Handles invalid golfer id gracefully."""
        rounds = get_player_rounds(session, -1)
        assert rounds == ()


class TestPlayerRoundsRoutes:
    """Route tests for player round history."""

    def test_player_rounds_route_requires_login(self, client: TestClient) -> None:
        """The /my/rounds route requires authentication."""
        response = client.get("/my/rounds", follow_redirects=False)
        assert response.status_code == 401

    def test_player_rounds_unlinked_player_403(self, client: TestClient) -> None:
        """A verified user not linked to a golfer gets 403."""
        with Session(client.app.state.engine) as session:
            _make_user(session, "unlinked@example.test", "pw!", None)
        _login(client, "unlinked@example.test", "pw!")
        response = client.get("/my/rounds", follow_redirects=False)
        assert response.status_code == 403
