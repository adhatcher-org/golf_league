"""F9: browsers get styled error pages; other clients keep FastAPI's JSON.

Status codes never change; only the body depends on the Accept header.
"""

import re
from datetime import UTC, datetime

from conftest import _extract_csrf
from sqlalchemy.orm import Session
from test_accessibility import _assert_accessible

from golf_league.models import User
from golf_league.services.auth import create_session_cookie, hash_password

HTML = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _user(client, *, verified: bool, admin: bool = False):
    with Session(client.app.state.engine) as session:
        user = User(username="viewer@example.test", email="viewer@example.test", display_name="Viewer",
                    password_hash=hash_password("synthetic-password"), is_admin=admin,
                    email_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None)
        session.add(user)
        session.commit()
        client.cookies.set("session", create_session_cookie(
            user.id, user.session_version, client.app.state.settings.session_secret))


def test_anonymous_browser_gets_login_page_and_api_client_gets_json(client):
    page = client.get("/admin/golfers", headers=HTML)
    assert page.status_code == 401
    assert "text/html" in page.headers["content-type"]
    assert "Please log in" in page.text and 'href="/login"' in page.text
    _assert_accessible("401 page", page.text)

    api = client.get("/admin/golfers", headers={"accept": "application/json"})
    assert api.status_code == 401 and api.json() == {"detail": "Not authenticated"}
    assert client.get("/admin/golfers").json() == {"detail": "Not authenticated"}


def test_unverified_user_gets_a_verification_page(client):
    _user(client, verified=False)
    page = client.get("/roster", headers=HTML)
    assert page.status_code == 403
    assert "Verify your email address" in page.text
    assert "{&#34;detail&#34;" not in page.text and '{"detail"' not in page.text


def test_verified_non_admin_gets_not_allowed_page(client):
    _user(client, verified=True)
    page = client.get("/admin/config", headers=HTML)
    assert page.status_code == 403 and "Not allowed" in page.text


def test_token_404_page_keeps_private_headers(client):
    page = client.get("/join/not-a-real-token", headers=HTML)
    assert page.status_code == 404 and "Page not found" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    _assert_accessible("404 page", page.text)


def test_conflict_page_shows_the_human_detail(admin_client):
    page = admin_client.get("/admin/invites/new")
    nonce = re.search(r'name="form_nonce" value="([^"]+)"', page.text).group(1)
    admin_client.post("/admin/invites/new", data={
        "label": "Conflict", "csrf_token": _extract_csrf(page.text), "form_nonce": nonce})
    listing = admin_client.get("/admin/invites")
    invite_id = re.search(r'action="/admin/invites/(\d+)/revoke"', listing.text).group(1)
    token = _extract_csrf(listing.text)
    admin_client.post(f"/admin/invites/{invite_id}/revoke", data={"csrf_token": token})
    conflict = admin_client.post(f"/admin/invites/{invite_id}/rotate", data={"csrf_token": token},
                                 headers=HTML, follow_redirects=False)
    assert conflict.status_code == 409
    assert "Invitation is expired or revoked" in conflict.text
    api = admin_client.post(f"/admin/invites/{invite_id}/rotate", data={"csrf_token": token},
                            follow_redirects=False)
    assert api.status_code == 409 and api.json() == {"detail": "Invitation is expired or revoked"}


def test_request_validation_error_page_for_browsers(admin_client):
    page = admin_client.get("/admin/seasons/not-a-number/edit", headers=HTML)
    assert page.status_code == 422 and "Check your request" in page.text
    api = admin_client.get("/admin/seasons/not-a-number/edit")
    assert api.status_code == 422 and "detail" in api.json()
