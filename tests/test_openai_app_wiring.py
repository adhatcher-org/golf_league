"""W3: App wiring tests for OpenAI provider support.

Uses TestClient (synchronous) like the existing vision tests; no asyncio needed.
"""

from fastapi.testclient import TestClient

from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.services.scorecard_vision import (
    OllamaVisionProvider,
    OpenAIVisionProvider,
    photo_entry_configured,
)


class FakeVisionProvider:
    """Fake provider for testing that assigned providers are never overwritten."""

    def read_scorecard(self, image_jpeg: bytes, *, hole_count: int):
        pass


def test_openai_provider_instantiation():
    """OpenAI configuration produces OpenAIVisionProvider instance."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="sk-test123",
        openai_model="gpt-4-vision",
        openai_base_url="https://api.openai.com/v1",
    )

    assert photo_entry_configured(settings) is True
    app = create_app(settings=settings)
    assert app.state.vision_provider is None  # nothing built before startup
    with TestClient(app) as client:
        provider = client.app.state.vision_provider
        assert isinstance(provider, OpenAIVisionProvider)
        assert provider._api_key == "sk-test123"
        assert provider._model == "gpt-4-vision"
        assert provider._base_url == "https://api.openai.com/v1"


def test_fake_provider_kept_before_startup():
    """Fake provider assigned before startup is kept."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="sk-test123",
        openai_model="gpt-4-vision",
        openai_base_url="https://api.openai.com/v1",
    )

    app = create_app(settings=settings)
    fake = FakeVisionProvider()
    app.state.vision_provider = fake
    with TestClient(app) as client:
        assert client.app.state.vision_provider is fake
        assert isinstance(client.app.state.vision_provider, FakeVisionProvider)


def test_fake_provider_kept_after_startup():
    """Fake provider assigned after startup is kept."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="sk-test123",
        openai_model="gpt-4-vision",
        openai_base_url="https://api.openai.com/v1",
    )

    app = create_app(settings=settings)
    with TestClient(app) as client:
        provider = client.app.state.vision_provider
        assert isinstance(provider, OpenAIVisionProvider)
        fake = FakeVisionProvider()
        client.app.state.vision_provider = fake
        assert client.app.state.vision_provider is fake


def test_bad_openai_values_leave_provider_none():
    """Bad OpenAI values leave vision_provider as None; /readyz still 200."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="",  # Missing API key
        openai_model="gpt-4-vision",
        openai_base_url="https://api.openai.com/v1",
    )

    assert photo_entry_configured(settings) is False
    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert client.app.state.vision_provider is None
        assert client.get("/readyz").status_code == 200


def test_ollama_provider_still_works():
    """Ollama provider still works as before."""
    settings = Settings(
        session_secret="test",
        analysis_provider="ollama",
        ollama_url="http://localhost:11434",
        ollama_vision_model="test-model",
        ollama_vision_num_ctx="32768",
    )

    assert photo_entry_configured(settings) is True
    app = create_app(settings=settings)
    with TestClient(app) as client:
        provider = client.app.state.vision_provider
        assert isinstance(provider, OllamaVisionProvider)
        assert provider._model == "test-model"
