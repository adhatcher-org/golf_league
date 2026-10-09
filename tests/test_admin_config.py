"""GL-44: managed configuration (R-DEPLOYMENT allowlist) — file, settings and /admin/config.

Every test uses a temporary managed file (see the autouse fixture in
conftest.py). Values are synthetic; no real relay, address or credential.
"""

import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import _extract_csrf
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from golf_league import managed_config
from golf_league.config import Settings
from golf_league.managed_config import (
    MANAGED_KEYS,
    MANAGED_KEYS_BY_NAME,
    read_managed_values,
    validate_value,
    write_managed_values,
)
from golf_league.models import User
from golf_league.services.auth import create_session_cookie, hash_password

SECRET = "synthetic-relay-secret-7f3a"


def _key(name):
    return MANAGED_KEYS_BY_NAME[name]


def _managed_path(client: TestClient) -> Path:
    return Path(client.app.state.settings.managed_env_path)


def _form(client: TestClient, **overrides) -> dict[str, str]:
    """The fields the real page submits, with its current values."""
    page = client.get("/admin/config")
    assert page.status_code == 200
    data = {"csrf_token": _extract_csrf(page.text)}
    settings = client.app.state.settings
    for key in MANAGED_KEYS:
        data[key.name] = "" if key.is_secret else str(getattr(settings, key.field))
    data.update(overrides)
    return data


# --- the allowlist ---------------------------------------------------------


def test_allowlist_is_exactly_the_eight_ruled_keys():
    assert [key.name for key in MANAGED_KEYS] == [
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
        "SMTP_FROM_EMAIL", "SMTP_FROM_NAME", "SMTP_TLS_MODE", "LOG_LEVEL",
    ]
    fixed = {"DATABASE_URL", "SESSION_SECRET", "EXTERNAL_BASE_URL", "MAX_USERS",
             "ADMIN_EMAIL", "ADMIN_PASSWORD", "MANAGED_ENV_PATH"}
    assert fixed.isdisjoint(MANAGED_KEYS_BY_NAME)
    assert [key.name for key in MANAGED_KEYS if key.is_secret] == ["SMTP_PASSWORD"]


@pytest.mark.parametrize("name,raw,expected", [
    ("SMTP_HOST", " relay.example.test ", "relay.example.test"),
    ("SMTP_HOST", "192.0.2.10", "192.0.2.10"),
    ("SMTP_HOST", "2001:db8::1", "2001:db8::1"),
    ("SMTP_HOST", "", ""),
    ("SMTP_PORT", "587", "587"),
    ("SMTP_PORT", "1", "1"),
    ("SMTP_PORT", "65535", "65535"),
    ("SMTP_PORT", "0465", "465"),
    ("SMTP_USERNAME", "", ""),
    ("SMTP_PASSWORD", " spaced secret ", " spaced secret "),
    ("SMTP_FROM_EMAIL", "league@example.test", "league@example.test"),
    ("SMTP_FROM_NAME", 'Synthetic "League", Mail', 'Synthetic "League", Mail'),
    ("SMTP_TLS_MODE", "SSL", "ssl"),
    ("SMTP_TLS_MODE", "none", "none"),
    ("LOG_LEVEL", "debug", "DEBUG"),
])
def test_valid_values_are_normalized(name, raw, expected):
    assert validate_value(_key(name), raw) == (expected, None)


@pytest.mark.parametrize("name,raw", [
    ("SMTP_HOST", "relay example.test"),
    ("SMTP_HOST", "-relay.example.test"),
    ("SMTP_HOST", "relay.example.test/path"),
    ("SMTP_HOST", "$(touch /tmp/x)"),
    ("SMTP_HOST", "a" * 254),
    ("SMTP_PORT", "0"),
    ("SMTP_PORT", "65536"),
    ("SMTP_PORT", "-1"),
    ("SMTP_PORT", "5.5"),
    ("SMTP_PORT", "²"),
    ("SMTP_PORT", ""),
    ("SMTP_FROM_EMAIL", "not-an-address"),
    ("SMTP_FROM_EMAIL", ""),
    ("SMTP_FROM_NAME", ""),
    ("SMTP_FROM_NAME", "x" * 121),
    ("SMTP_TLS_MODE", "plaintext-upgrade"),
    ("LOG_LEVEL", "TRACE"),
    ("SMTP_USERNAME", "line\nbreak"),
    ("SMTP_PASSWORD", "line\rbreak"),
    ("SMTP_PASSWORD", "nul\x00byte"),
])
def test_invalid_values_are_rejected_without_echoing_them(name, raw):
    value, error = validate_value(_key(name), raw)
    assert value == ""
    assert error
    if raw.strip():
        assert raw.strip() not in error


# --- reading the file --------------------------------------------------------


def test_missing_file_reads_as_empty(tmp_path):
    assert read_managed_values(tmp_path / "absent.env") == {}


def test_unreadable_file_reads_as_empty(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert read_managed_values(tmp_path) == {}
    assert "could not be read" in caplog.text


def test_reader_ignores_unknown_keys_and_invalid_values(tmp_path, caplog):
    path = tmp_path / ".env"
    path.write_text(
        "# operator comment\n"
        "DATABASE_URL=sqlite:////tmp/elsewhere.db\n"
        "SESSION_SECRET=synthetic-should-not-load\n"
        "SMTP_HOST=\"relay.example.test\"\n"
        "SMTP_PORT=not-a-port\n"
        "export SMTP_TLS_MODE='ssl'\n"
        "SMTP_FROM_NAME=\"Quote \\\" and \\\\ slash\"\n"
        "LOG_LEVEL=INFO\n"
        "LOG_LEVEL=ERROR\n"
        "garbage line without equals\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        values = read_managed_values(path)
    assert values == {
        "SMTP_HOST": "relay.example.test",
        "SMTP_TLS_MODE": "ssl",
        "SMTP_FROM_NAME": 'Quote " and \\ slash',
        "LOG_LEVEL": "ERROR",
    }
    assert "SMTP_PORT" in caplog.text
    assert "not-a-port" not in caplog.text


# --- writing the file --------------------------------------------------------


def test_write_creates_file_with_owner_only_mode(tmp_path):
    path = tmp_path / ".env"
    write_managed_values(path, {"SMTP_HOST": "relay.example.test"})
    assert read_managed_values(path) == {"SMTP_HOST": "relay.example.test"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_preserves_unowned_lines_and_replaces_in_place(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# kept comment\n"
        "UNRELATED_KEY=keep-me\n"
        "SMTP_HOST=old.example.test\n"
        "\n"
        "SMTP_HOST=duplicate.example.test\n"
        "OTHER=value with spaces",
        encoding="utf-8",
    )
    os.chmod(path, 0o644)
    write_managed_values(path, {"SMTP_HOST": "new.example.test", "LOG_LEVEL": "WARNING"})
    assert path.read_text(encoding="utf-8") == (
        "# kept comment\n"
        "UNRELATED_KEY=keep-me\n"
        'SMTP_HOST="new.example.test"\n'
        "\n"
        "OTHER=value with spaces\n"
        'LOG_LEVEL="WARNING"\n'
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_written_values_round_trip_awkward_characters(tmp_path):
    path = tmp_path / ".env"
    awkward = 'p"a\\ss #word $HOME `x` \'q\''
    write_managed_values(path, {"SMTP_PASSWORD": awkward, "SMTP_FROM_NAME": "Synthetic, \"League\""})
    assert read_managed_values(path) == {
        "SMTP_PASSWORD": awkward, "SMTP_FROM_NAME": "Synthetic, \"League\"",
    }


@pytest.mark.parametrize("updates", [
    {"DATABASE_URL": "sqlite:////tmp/x.db"},
    {"MANAGED_ENV_PATH": "/tmp/elsewhere"},
    {"SMTP_PORT": "70000"},
    {"SMTP_PORT": "0587"},
    {"SMTP_HOST": "bad host"},
])
def test_write_refuses_unknown_keys_and_invalid_values_before_touching_file(tmp_path, updates):
    path = tmp_path / ".env"
    path.write_text("SMTP_HOST=keep.example.test\n", encoding="utf-8")
    with pytest.raises(ValueError):
        write_managed_values(path, updates)
    assert path.read_text(encoding="utf-8") == "SMTP_HOST=keep.example.test\n"


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_interrupted_write_leaves_previous_file_intact(tmp_path, monkeypatch, failing):
    path = tmp_path / ".env"
    original = "UNRELATED=1\nSMTP_HOST=keep.example.test\n"
    path.write_text(original, encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(managed_config.os, failing, boom)
    with pytest.raises(OSError):
        write_managed_values(path, {"SMTP_HOST": "new.example.test"})
    assert path.read_text(encoding="utf-8") == original
    assert sorted(item.name for item in tmp_path.iterdir()) == [".env"]


# --- Settings precedence ------------------------------------------------------


def test_settings_defaults_match_the_ruling(monkeypatch):
    monkeypatch.delenv("MANAGED_ENV_PATH")
    settings = Settings(session_secret="synthetic", managed_env_path="/nonexistent/.env")
    assert (settings.smtp_host, settings.smtp_port, settings.smtp_username, settings.smtp_password) == ("", 587, "", "")
    assert settings.smtp_tls_mode == "starttls"
    assert settings.log_level == "INFO"
    assert Settings.model_fields["managed_env_path"].default == "./data/.env"


def test_managed_file_outranks_environment_which_outranks_default(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('SMTP_HOST="file.example.test"\nSMTP_PORT="not valid"\n', encoding="utf-8")
    monkeypatch.setenv("MANAGED_ENV_PATH", str(path))
    monkeypatch.setenv("SMTP_HOST", "env.example.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_TLS_MODE", "SSL")

    settings = Settings(session_secret="synthetic")

    assert settings.smtp_host == "file.example.test"
    assert settings.smtp_port == 2525  # invalid file value falls back to the environment
    assert settings.smtp_tls_mode == "ssl"
    assert settings.smtp_from_name == "St. Paul Golf League"  # code default


def test_managed_file_cannot_set_fixed_deploy_time_settings(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "DATABASE_URL=sqlite:////tmp/evil.db\nSESSION_SECRET=from-file\n"
        "EXTERNAL_BASE_URL=https://evil.example.test\nMAX_USERS=9999\n"
        "MANAGED_ENV_PATH=/tmp/other.env\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MANAGED_ENV_PATH", str(path))
    settings = Settings(session_secret="synthetic")
    assert settings.database_url == "sqlite:///./data/golf_league.db"
    assert settings.session_secret == "synthetic"
    assert settings.external_base_url == "https://golfleague.aaronhatcher.com"
    assert settings.max_users == 150
    assert settings.managed_env_path == str(path)


def test_smtp_password_is_not_in_settings_repr(tmp_path):
    settings = Settings(session_secret="synthetic", smtp_password=SECRET,
                        managed_env_path=str(tmp_path / ".env"))
    assert SECRET not in repr(settings)


# --- /admin/config over HTTP --------------------------------------------------


def _player_client(client: TestClient) -> TestClient:
    with Session(client.app.state.engine) as session:
        user = User(
            username="player@example.test", email="player@example.test", display_name="Player",
            password_hash=hash_password("synthetic-password"),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=False,
        )
        session.add(user)
        session.commit()
        cookie = create_session_cookie(user.id, user.session_version,
                                       client.app.state.settings.session_secret)
    client.cookies.set("session", cookie)
    return client


def test_anonymous_and_nonadmin_are_refused(client):
    assert client.get("/admin/config").status_code == 401
    assert client.post("/admin/config", data={"SMTP_HOST": "x.example.test"}).status_code == 401

    _player_client(client)
    assert client.get("/admin/config").status_code == 403
    response = client.post("/admin/config", data={"SMTP_HOST": "x.example.test", "csrf_token": "x"})
    assert response.status_code == 403
    assert not _managed_path(client).exists()


def test_admin_sees_allowlisted_fields_without_secret_values(admin_client, tmp_path):
    write_managed_values(_managed_path(admin_client), {"SMTP_PASSWORD": SECRET})
    response = admin_client.get("/admin/config")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    for key in MANAGED_KEYS:
        assert f'name="{key.name}"' in html
        assert f'<label for="config-{key.name.lower()}">' in html
    assert SECRET not in html
    assert "Currently set." in html
    assert 'name="DATABASE_URL"' not in html and 'name="SESSION_SECRET"' not in html
    assert 'href="/admin/config"' in html  # reachable from the Manage menu


@pytest.mark.parametrize("token", [None, "", "forged-token-value"])
def test_csrf_is_required(admin_client, token):
    data = _form(admin_client, SMTP_HOST="relay.example.test")
    if token is None:
        del data["csrf_token"]
    else:
        data["csrf_token"] = token
    response = admin_client.post("/admin/config", data=data, follow_redirects=False)
    assert response.status_code == 403
    assert not _managed_path(admin_client).exists()


@pytest.mark.parametrize("extra", [
    {"DATABASE_URL": "sqlite:////tmp/evil.db"},
    {"SESSION_SECRET": "synthetic"},
    {"MANAGED_ENV_PATH": "/tmp/elsewhere.env"},
    {"PATH": "/tmp"},
])
def test_unknown_keys_are_rejected_and_nothing_is_written(admin_client, extra):
    data = _form(admin_client, SMTP_HOST="relay.example.test", **extra)
    response = admin_client.post("/admin/config", data=data, follow_redirects=False)
    assert response.status_code == 422
    assert "There is a problem" in response.text
    assert not _managed_path(admin_client).exists()


def test_duplicate_fields_are_rejected(admin_client):
    data = list(_form(admin_client).items()) + [("SMTP_HOST", "second.example.test")]
    response = admin_client.post(
        "/admin/config", content="&".join(f"{k}={v}" for k, v in data),
        headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False,
    )
    assert response.status_code == 422
    assert not _managed_path(admin_client).exists()


def test_invalid_values_rerender_stored_values_with_error_summary(admin_client):
    data = _form(admin_client, SMTP_HOST="bad host name", SMTP_PORT="70000", LOG_LEVEL="LOUD")
    response = admin_client.post("/admin/config", data=data, follow_redirects=False)
    assert response.status_code == 422
    html = response.text
    assert 'role="alert"' in html and "There is a problem" in html
    assert 'href="#config-smtp_port"' in html
    assert 'aria-invalid="true"' in html
    assert "bad host name" not in html and "70000" not in html
    assert not _managed_path(admin_client).exists()


def test_success_writes_only_changed_keys_and_is_restart_required(admin_client, caplog):
    data = _form(admin_client, SMTP_HOST="relay.example.test", SMTP_USERNAME="synthetic-user",
                 SMTP_PASSWORD=SECRET, SMTP_PORT="2525", SMTP_TLS_MODE="ssl", LOG_LEVEL="warning")
    with caplog.at_level(logging.INFO):
        response = admin_client.post("/admin/config", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/config?saved=1"

    path = _managed_path(admin_client)
    assert read_managed_values(path) == {
        "SMTP_HOST": "relay.example.test", "SMTP_USERNAME": "synthetic-user",
        "SMTP_PASSWORD": SECRET, "SMTP_PORT": "2525", "SMTP_TLS_MODE": "ssl", "LOG_LEVEL": "WARNING",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SECRET not in caplog.text and "synthetic-user" not in caplog.text
    assert "relay.example.test" not in caplog.text

    # The running process is unchanged until restart.
    assert admin_client.app.state.settings.smtp_host == ""
    page = admin_client.get("/admin/config?saved=1")
    assert "Saved." in page.text
    assert page.text.count("Restart required:") == 6
    assert SECRET not in page.text
    assert 'value="relay.example.test"' in page.text


def test_saved_setting_survives_restart(admin_client):
    data = _form(admin_client, SMTP_HOST="relay.example.test", SMTP_FROM_NAME="Synthetic League")
    assert admin_client.post("/admin/config", data=data, follow_redirects=False).status_code == 303

    restarted = Settings(session_secret="synthetic",
                         managed_env_path=admin_client.app.state.settings.managed_env_path)
    assert restarted.smtp_host == "relay.example.test"
    assert restarted.smtp_from_name == "Synthetic League"


def test_unchanged_form_writes_nothing(admin_client):
    response = admin_client.post("/admin/config", data=_form(admin_client), follow_redirects=False)
    assert response.status_code == 303
    assert not _managed_path(admin_client).exists()


def test_blank_secret_keeps_and_clear_empties(admin_client):
    path = _managed_path(admin_client)
    write_managed_values(path, {"SMTP_PASSWORD": SECRET})

    response = admin_client.post("/admin/config", data=_form(admin_client, SMTP_HOST="relay.example.test"),
                                 follow_redirects=False)
    assert response.status_code == 303
    assert read_managed_values(path)["SMTP_PASSWORD"] == SECRET

    both = _form(admin_client, SMTP_PASSWORD="another-synthetic", SMTP_PASSWORD__clear="1")
    response = admin_client.post("/admin/config", data=both, follow_redirects=False)
    assert response.status_code == 422
    assert "not both" in response.text
    assert read_managed_values(path)["SMTP_PASSWORD"] == SECRET

    response = admin_client.post("/admin/config", data=_form(admin_client, SMTP_PASSWORD__clear="1"),
                                 follow_redirects=False)
    assert response.status_code == 303
    assert read_managed_values(path)["SMTP_PASSWORD"] == ""
    assert "Currently not set." in admin_client.get("/admin/config").text


def test_write_failure_keeps_file_and_reports_without_values(admin_client, monkeypatch, caplog):
    path = _managed_path(admin_client)
    write_managed_values(path, {"SMTP_HOST": "keep.example.test"})
    before = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated full disk")

    monkeypatch.setattr(managed_config.os, "replace", boom)
    data = _form(admin_client, SMTP_HOST="new.example.test", SMTP_PASSWORD=SECRET)
    with caplog.at_level(logging.INFO):
        response = admin_client.post("/admin/config", data=data, follow_redirects=False)
    assert response.status_code == 503
    assert "Nothing was changed" in response.text
    assert path.read_text(encoding="utf-8") == before
    assert "managed configuration write failed" in caplog.text
    assert SECRET not in caplog.text and "simulated full disk" not in caplog.text
    assert SECRET not in response.text


def test_lifespan_applies_managed_log_level(tmp_path):
    from golf_league.app import create_app

    app_logger = logging.getLogger("golf_league")
    previous = app_logger.level
    try:
        settings = Settings(database_url=f"sqlite:///{tmp_path / 'app.db'}", session_secret="synthetic",
                            seed_course=False, log_level="error")
        with TestClient(create_app(settings=settings)):
            assert app_logger.level == logging.ERROR
    finally:
        app_logger.setLevel(previous)
