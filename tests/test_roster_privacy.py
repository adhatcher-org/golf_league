"""Verified player roster is access-controlled and does not expose contacts."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import Golfer, TeeSet, User
from golf_league.services.auth import create_session_cookie, hash_password


def _add_golfer(client, *, active=True):
    session = Session(bind=client.app.state.engine)
    try:
        tee = session.execute(select(TeeSet).where(TeeSet.name == "Deer")).scalar_one()
        session.add(Golfer(first_name="Visible", last_name="Player", email="private@example.test", phone="555-1234", notes="secret note", default_tee_set_id=tee.id, handicap_strokes=None, handicap_source="self_reported", handicap_status="needs_entry", is_active=active))
        session.commit()
    finally:
        session.close()


def _login_as(client, *, verified):
    email = "verified-viewer@example.test" if verified else "unverified-viewer@example.test"
    session = Session(bind=client.app.state.engine)
    try:
        user = User(username=email, email=email, display_name="Viewer", password_hash=hash_password("a-real-password-1"), email_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None)
        session.add(user)
        session.commit()
        session.refresh(user)
        client.cookies.set("session", create_session_cookie(user.id, user.session_version, client.app.state.settings.session_secret))
    finally:
        session.close()


def test_roster_requires_verified_user_and_exposes_only_projection(client):
    _add_golfer(client)
    assert client.get("/roster").status_code == 401
    _login_as(client, verified=False)
    assert client.get("/roster").status_code == 403
    _login_as(client, verified=True)
    response = client.get("/roster")
    assert response.status_code == 200
    assert "Visible Player" in response.text and "No handicap on file" in response.text
    for forbidden in ("private@example.test", "555-1234", "secret note", "notes", "phone", "email"):
        assert forbidden not in response.text.lower()
