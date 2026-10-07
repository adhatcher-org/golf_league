"""Role-aware links in the shared site navigation."""

import re

from test_my_schedule import _seed


def _navigation(client):
    response = client.get("/login", headers={"accept": "text/html"})
    assert response.status_code == 200
    return response.text.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]


def test_anonymous_navigation_has_no_player_or_admin_links(client):
    nav = _navigation(client)

    assert 'href="/login"' in nav
    assert 'href="/my/schedule"' not in nav
    assert 'href="/roster"' not in nav
    assert "/admin/" not in nav
    assert "Manage" not in nav


def test_health_check_does_not_resolve_navigation_identity(client, monkeypatch):
    from golf_league import app as app_module

    def fail_if_called(*args, **kwargs):
        raise AssertionError("health checks must not resolve a user")

    monkeypatch.setattr(app_module, "get_optional_user", fail_if_called)
    client.cookies.set("session", "synthetic-session-cookie")

    response = client.get("/healthz", headers={"accept": "text/html"})

    assert response.status_code == 200


def test_normal_user_sees_schedule_and_logout_but_no_admin_menu(client):
    _seed(client)

    nav = _navigation(client)

    assert 'href="/my/schedule"' in nav
    assert 'href="/roster"' in nav
    assert "Manage" not in nav
    assert "/admin/" not in nav
    assert 'action="/logout"' in nav
    assert "Log out" in nav
    assert 'href="/login"' not in nav

    token = re.search(r'name="csrf_token" value="([^"]+)"', nav).group(1)
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert 'session=""' in response.headers["set-cookie"]


def test_admin_navigation_includes_manage_menu(client):
    _seed(client, admin=True)

    nav = _navigation(client)

    assert 'href="/my/schedule"' in nav
    assert "Manage" in nav
    assert 'href="/admin/golfers"' in nav
