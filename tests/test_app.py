from fastapi.testclient import TestClient


def test_create_app_uses_the_settings_it_is_given(client, tmp_path):
    """Test that create_app uses the settings it's given and creates the database."""
    # Test that /readyz returns 200
    response = client.get("/readyz")
    assert response.status_code == 200

    # Test that the database file was created at the expected location
    db_path = tmp_path / "app.db"
    assert db_path.exists(), f"Database file should exist at {db_path}"
    assert db_path.is_file(), f"{db_path} should be a file"

    # Verify the database is not empty (has at least the schema)
    assert db_path.stat().st_size > 0, "Database file should not be empty"


def _client_with(tmp_path, **overrides):
    from golf_league.app import create_app
    from golf_league.config import Settings

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'mail.db'}", session_secret="synthetic",
                        seed_course=False, **overrides)
    return TestClient(create_app(settings=settings))


def test_smtp_host_installs_the_smtp_sender_at_startup(tmp_path):
    from golf_league.services.email import SmtpEmailSender

    with _client_with(tmp_path, smtp_host="relay.example.test", smtp_port=2525, smtp_tls_mode="ssl",
                      smtp_username="synthetic-user", smtp_password="synthetic-secret") as client:
        sender = client.app.state.email_sender
        assert isinstance(sender, SmtpEmailSender)
        assert (sender.host, sender.port, sender.tls_mode) == ("relay.example.test", 2525, "ssl")
        assert "synthetic-secret" not in repr(sender)


def test_without_smtp_host_the_fake_sender_stays(tmp_path):
    from golf_league.services.email import FakeEmailSender

    with _client_with(tmp_path) as client:
        assert isinstance(client.app.state.email_sender, FakeEmailSender)


def test_importing_the_app_needs_no_session_secret():
    import os
    import subprocess
    import sys

    env = {key: value for key, value in os.environ.items() if key != "SESSION_SECRET"}
    result = subprocess.run([sys.executable, "-c", "import golf_league.app"], env=env,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
