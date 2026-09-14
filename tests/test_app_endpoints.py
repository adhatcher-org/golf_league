from fastapi.testclient import TestClient


def test_app_healthz_endpoint(client):
    """Test that /healthz returns 200 with correct response."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_readyz_endpoint_when_ready(client):
    """Test that /readyz returns 200 when the app is ready."""
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_503_when_the_database_cannot_be_opened(tmp_path):
    """Test that /readyz returns 503 when the database path cannot be opened."""
    from golf_league.app import create_app
    from golf_league.config import Settings

    # Point the database at a path whose parent is a file, not a directory,
    # so SQLite genuinely cannot open it. Uses this test's own tmp_path so no
    # test shares a database with another.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    settings = Settings(
        database_url=f"sqlite:///{blocker}/app.db",
        session_secret="test-only-not-a-secret",
    )

    with TestClient(create_app(settings=settings)) as test_client:
        # /healthz should still work even if the database is unavailable
        assert test_client.get("/healthz").status_code == 200

        # /readyz must fail because the database cannot be opened
        assert test_client.get("/readyz").status_code == 503


def test_app_without_settings_uses_default():
    """Test that create_app without settings uses default configuration."""
    from golf_league.app import create_app

    # This should not raise an error and should create an app
    app = create_app()
    assert app is not None

    # Test that healthz works
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
