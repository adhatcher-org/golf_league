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


def test_trusted_proxies_come_from_environment_then_managed_file_then_default(tmp_path, caplog):
    from golf_league.managed_config import trusted_proxy_addresses

    path = tmp_path / ".env"
    assert trusted_proxy_addresses({}, path) == ("127.0.0.1", "default")  # no file at all
    path.write_text('FORWARDED_ALLOW_IPS="192.0.2.10"\nSMTP_HOST=relay.example.test\n', encoding="utf-8")
    assert trusted_proxy_addresses({}, path) == ("192.0.2.10", "managed file")
    assert trusted_proxy_addresses({"FORWARDED_ALLOW_IPS": " "}, path) == ("192.0.2.10", "managed file")
    assert trusted_proxy_addresses({"FORWARDED_ALLOW_IPS": "198.51.100.7"}, path) == ("198.51.100.7", "environment")
    path.write_text("FORWARDED_ALLOW_IPS=\n", encoding="utf-8")  # empty counts as unset
    assert trusted_proxy_addresses({}, path) == ("127.0.0.1", "default")
    for everyone in ("*", "192.0.2.10, *"):
        path.write_text(f"FORWARDED_ALLOW_IPS={everyone}\n", encoding="utf-8")
        assert trusted_proxy_addresses({}, path) == ("127.0.0.1", "default")
    assert "trusts every address" in caplog.text


def test_main_serves_with_the_trusted_proxies_from_the_managed_file(tmp_path, monkeypatch, capsys):
    import uvicorn

    import golf_league.app as app_module

    path = tmp_path / ".env"
    path.write_text("FORWARDED_ALLOW_IPS=192.0.2.10\n", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ENV_PATH", str(path))
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    app_module.main()

    ((served, kwargs),) = calls
    assert served is app_module.app
    assert kwargs == {"host": "0.0.0.0", "port": 8000, "proxy_headers": True, "forwarded_allow_ips": "192.0.2.10"}
    assert "managed file" in capsys.readouterr().out
