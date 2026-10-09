"""Session and CSRF cookies carry `Secure` exactly when EXTERNAL_BASE_URL is https.

The flag comes from configuration only (R-DEPLOYMENT), never from the
request scheme, Host or forwarded headers. An http origin is the local
development case and must keep working over plain http.
"""

from datetime import UTC, datetime

import pytest
from conftest import _extract_csrf
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.models import User
from golf_league.services.auth import hash_password

EMAIL = "cookie-admin@example.test"
PASSWORD = "synthetic-cookie-password"


def _client(tmp_path, origin: str, base_url: str) -> TestClient:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'cookies.db'}", session_secret="synthetic",
                        seed_course=False, external_base_url=origin)
    return TestClient(create_app(settings=settings), base_url=base_url)


def _add_admin(client: TestClient) -> None:
    with Session(bind=client.app.state.engine) as session:
        session.add(User(username=EMAIL, email=EMAIL, display_name="Admin",
                         password_hash=hash_password(PASSWORD),
                         email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=True))
        session.commit()


def _cookie_header(response, name: str) -> str:
    headers = [value for value in response.headers.get_list("set-cookie") if value.startswith(f"{name}=")]
    assert len(headers) == 1, response.headers.get_list("set-cookie")
    return headers[0]


def _is_secure(header: str) -> bool:
    return any(part.strip().lower() == "secure" for part in header.split(";"))


def _login(client: TestClient, headers=None):
    page = client.get("/login", headers=headers or {})
    response = client.post("/login", data={"email": EMAIL, "password": PASSWORD,
                                           "csrf_token": _extract_csrf(page.text)},
                           headers=headers or {}, follow_redirects=False)
    return page, response


def test_https_origin_marks_csrf_session_and_logout_cookies_secure(tmp_path):
    with _client(tmp_path, "https://league.example.test", "https://testserver") as client:
        _add_admin(client)
        page, response = _login(client)
        assert response.status_code == 303
        csrf_cookie = _cookie_header(page, "csrf_seed")
        session_cookie = _cookie_header(response, "session")
        for header in (csrf_cookie, session_cookie):
            assert _is_secure(header) and "httponly" in header.lower() and "samesite=lax" in header.lower()
        assert client.get("/admin/config").status_code == 200  # the Secure cookie is sent back

        nav = client.get("/login").text
        token = nav.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        logout = client.post("/logout", data={"csrf": token}, follow_redirects=False)
        assert _is_secure(_cookie_header(logout, "session"))


@pytest.mark.parametrize("path", ["/reset", "/register"])
def test_https_origin_marks_anonymous_form_seed_cookies_secure(tmp_path, path):
    with _client(tmp_path, "https://league.example.test", "https://testserver") as client:
        assert _is_secure(_cookie_header(client.get(path), "csrf_seed"))


def test_https_origin_is_secure_even_when_the_proxy_hop_is_plain_http(tmp_path):
    # Behind SWAG the app sees plain HTTP; the flag still follows the configured origin.
    with _client(tmp_path, "https://league.example.test", "http://testserver") as client:
        assert _is_secure(_cookie_header(client.get("/login"), "csrf_seed"))


def test_http_origin_keeps_plain_cookies_for_local_development(tmp_path):
    with _client(tmp_path, "http://localhost:8000", "http://testserver") as client:
        _add_admin(client)
        page, response = _login(client, headers={"x-forwarded-proto": "https"})
        assert response.status_code == 303
        assert not _is_secure(_cookie_header(page, "csrf_seed"))
        assert not _is_secure(_cookie_header(response, "session"))
        assert client.get("/admin/config").status_code == 200
