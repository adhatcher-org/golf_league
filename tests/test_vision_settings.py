"""GL-80: the scorecard photo settings, their interpreting helpers and the startup wiring.

All values are synthetic placeholders; nothing here touches a network or a real `.env`.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.managed_config import DEPLOY_FILE_KEYS, MANAGED_KEYS_BY_NAME
from golf_league.services import scorecard_vision
from golf_league.services.scorecard_vision import (
    OllamaVisionProvider,
    analysis_provider,
    league_timezone_name,
    photo_entry_configured,
    vision_num_ctx,
)

URL = "http://ollama.example.invalid:11434"
MODEL = "example-model:1b"
KEYS = ("ANALYSIS_PROVIDER", "OLLAMA_URL", "OLLAMA_VISION_MODEL", "OLLAMA_VISION_NUM_CTX",
        "LEAGUE_TIMEZONE")


def _settings(tmp_path=None, **overrides) -> Settings:
    values = {"session_secret": "synthetic-test-secret"}
    if tmp_path is not None:
        values["database_url"] = f"sqlite:///{tmp_path / 'app.db'}"
        values["seed_course"] = False
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _reset_timezone_warning(monkeypatch):
    monkeypatch.setattr(scorecard_vision, "_timezone_warning_logged", False)


# --- defaults -------------------------------------------------------------------


def test_defaults():
    settings = _settings()
    assert analysis_provider(settings) == "ollama"
    assert settings.ollama_url == "" and settings.ollama_vision_model == ""
    assert vision_num_ctx(settings) == 32768
    assert league_timezone_name(settings) == "America/Chicago"
    assert not photo_entry_configured(settings)


def test_url_alone_is_not_configured_and_all_three_are():
    assert not photo_entry_configured(_settings(ollama_url=URL))
    assert not photo_entry_configured(_settings(ollama_vision_model=MODEL))
    assert photo_entry_configured(_settings(ollama_url=URL, ollama_vision_model=MODEL))


# --- precedence --------------------------------------------------------------------


def test_deploy_keys_are_registered_but_never_managed():
    for name in KEYS:
        assert DEPLOY_FILE_KEYS[name] == name.lower()
        assert name not in MANAGED_KEYS_BY_NAME


def test_environment_overrides_the_managed_files_deploy_section(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "SESSION_SECRET=synthetic-file-secret\n"
        "OLLAMA_URL=http://file.example.invalid:1\nOLLAMA_VISION_MODEL=file-model\n"
        "OLLAMA_VISION_NUM_CTX=4096\nLEAGUE_TIMEZONE=America/New_York\nANALYSIS_PROVIDER=openai\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MANAGED_ENV_PATH", str(path))
    monkeypatch.delenv("SESSION_SECRET", raising=False)

    from_file = Settings()
    assert from_file.ollama_url == "http://file.example.invalid:1"
    assert from_file.ollama_vision_model == "file-model"
    assert vision_num_ctx(from_file) == 4096
    assert league_timezone_name(from_file) == "America/New_York"
    assert analysis_provider(from_file) == "openai"

    monkeypatch.setenv("OLLAMA_URL", URL)
    monkeypatch.setenv("OLLAMA_VISION_NUM_CTX", "8192")
    monkeypatch.setenv("ANALYSIS_PROVIDER", "ollama")
    from_env = Settings()
    assert from_env.ollama_url == URL
    assert vision_num_ctx(from_env) == 8192
    assert from_env.ollama_vision_model == "file-model"  # environment unset: file applies
    assert photo_entry_configured(from_env)


def test_empty_environment_value_counts_as_unset(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(f"SESSION_SECRET=synthetic-file-secret\nOLLAMA_URL={URL}\n", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ENV_PATH", str(path))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "")
    assert Settings().ollama_url == URL


# --- interpretation -----------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (" OLLAMA ", "ollama"), ("", "ollama"), ("   ", "ollama"), ("openai", "openai"),
    (" OpenAI ", "openai"), ("other", "other"),
])
def test_analysis_provider_normalises(value, expected):
    assert analysis_provider(_settings(analysis_provider=value)) == expected


def test_provider_value_cases_for_configured():
    assert photo_entry_configured(_settings(analysis_provider=" OLLAMA ", ollama_url=URL,
                                            ollama_vision_model=MODEL))
    assert not photo_entry_configured(_settings(analysis_provider="openai", ollama_url=URL,
                                                ollama_vision_model=MODEL))


@pytest.mark.parametrize("url", ["", "   ", "http://", "http:///path", "ftp://x", "ollama:11434",
                                 "http://host:notaport", "//host", "not a url"])
def test_bad_urls_are_not_configured(url):
    settings = _settings(ollama_url=url, ollama_vision_model=MODEL)
    assert not photo_entry_configured(settings)


@pytest.mark.parametrize("url", ["http://x", "https://ollama.example.invalid", " http://h:1/ "])
def test_good_urls_are_configured(url):
    assert photo_entry_configured(_settings(ollama_url=url, ollama_vision_model=MODEL))


@pytest.mark.parametrize("value,expected", [
    ("", 32768), ("  ", 32768), ("2048", 2048), ("131072", 131072), (" 4096 ", 4096),
    ("abc", None), ("100", None), ("2047", None), ("131073", None), ("999999", None),
    ("-4096", None), ("4096.0", None), ("٤٠٩٦", None), ("²048", None),
    ("99999999999999999999999", None),
])
def test_vision_num_ctx(value, expected):
    settings = _settings(ollama_vision_num_ctx=value)
    assert vision_num_ctx(settings) == expected
    configured = photo_entry_configured(
        _settings(ollama_url=URL, ollama_vision_model=MODEL, ollama_vision_num_ctx=value)
    )
    assert configured is (expected is not None)


def test_unknown_timezone_falls_back_and_warns_exactly_once(caplog):
    settings = _settings(league_timezone="Not/AZone-secret-value")
    with caplog.at_level(logging.WARNING, logger=scorecard_vision.logger.name):
        assert league_timezone_name(settings) == "America/Chicago"
        assert league_timezone_name(settings) == "America/Chicago"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "LEAGUE_TIMEZONE" in warnings[0].getMessage()
    assert "Not/AZone" not in caplog.text


@pytest.mark.parametrize("value", ["", "  ", "../etc/passwd", "Mars/Olympus"])
def test_invalid_timezone_values_fall_back(value):
    assert league_timezone_name(_settings(league_timezone=value)) == "America/Chicago"


def test_valid_timezone_is_returned_stripped():
    assert league_timezone_name(_settings(league_timezone=" America/New_York ")) == "America/New_York"


def test_settings_never_raise_for_the_five_keys():
    settings = _settings(analysis_provider="???", ollama_url="\x00", ollama_vision_model="",
                         ollama_vision_num_ctx="abc", league_timezone="nope")
    assert not photo_entry_configured(settings)
    assert vision_num_ctx(settings) is None
    assert league_timezone_name(settings) == "America/Chicago"


# --- app wiring -------------------------------------------------------------------------


def test_app_starts_with_every_bad_value(tmp_path):
    settings = _settings(tmp_path, analysis_provider="openai", ollama_url="ftp://x",
                         ollama_vision_num_ctx="abc", league_timezone="nope")
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/readyz").status_code == 200
        assert client.app.state.vision_provider is None


def test_assigned_fake_before_startup_is_kept(tmp_path):
    fake = object()
    app = create_app(settings=_settings(tmp_path, ollama_url=URL, ollama_vision_model=MODEL))
    app.state.vision_provider = fake
    with TestClient(app) as client:
        assert client.app.state.vision_provider is fake


def test_fake_assigned_after_startup_replaces_the_provider(tmp_path):
    fake = object()
    with TestClient(create_app(settings=_settings(tmp_path, ollama_url=URL,
                                                  ollama_vision_model=MODEL))) as client:
        assert isinstance(client.app.state.vision_provider, OllamaVisionProvider)
        client.app.state.vision_provider = fake
        assert client.app.state.vision_provider is fake


def test_configured_settings_build_an_ollama_provider(tmp_path):
    settings = _settings(tmp_path, ollama_url=URL, ollama_vision_model=MODEL,
                         ollama_vision_num_ctx="4096")
    app = create_app(settings=settings)
    assert app.state.vision_provider is None  # nothing is built before startup
    with TestClient(app) as client:
        provider = client.app.state.vision_provider
        assert isinstance(provider, OllamaVisionProvider)
        assert client.get("/healthz").status_code == 200


def test_unconfigured_settings_leave_the_provider_none(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path, ollama_url=URL))) as client:
        assert client.app.state.vision_provider is None
        assert client.get("/readyz").status_code == 200
