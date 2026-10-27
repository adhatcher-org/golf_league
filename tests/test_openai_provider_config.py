"""W2: Provider interface extension tests for OpenAI support."""
from golf_league.config import Settings
from golf_league.services.scorecard_vision import (
    analysis_provider,
    photo_entry_configured,
)


def test_photo_entry_configured_ollama():
    """Ollama configuration is still recognized."""
    settings = Settings(
        session_secret="test",
        analysis_provider="ollama",
        ollama_url="http://localhost:11434",
        ollama_vision_model="test-model",
        ollama_vision_num_ctx="32768",
    )
    assert photo_entry_configured(settings) is True


def test_photo_entry_configured_openai():
    """OpenAI configuration is recognized."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="sk-test123",
        openai_model="gpt-4-vision",
        openai_base_url="https://api.openai.com/v1",
    )
    assert photo_entry_configured(settings) is True


def test_photo_entry_configured_openai_missing_key():
    """OpenAI without API key is not configured."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="",
        openai_model="gpt-4-vision",
        openai_base_url="https://api.openai.com/v1",
    )
    assert photo_entry_configured(settings) is False


def test_photo_entry_configured_openai_missing_model():
    """OpenAI without model is not configured."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="sk-test123",
        openai_model="",
        openai_base_url="https://api.openai.com/v1",
    )
    assert photo_entry_configured(settings) is False


def test_photo_entry_configured_openai_missing_url():
    """OpenAI without base URL is not configured."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
        openai_api_key="sk-test123",
        openai_model="gpt-4-vision",
        openai_base_url="",
    )
    assert photo_entry_configured(settings) is False


def test_photo_entry_configured_unknown_provider():
    """Unknown provider is not configured."""
    settings = Settings(
        session_secret="test",
        analysis_provider="unknown",
    )
    assert photo_entry_configured(settings) is False


def test_photo_entry_configured_ollama_missing_fields():
    """Ollama with missing fields is not configured."""
    settings = Settings(
        session_secret="test",
        analysis_provider="ollama",
        ollama_url="",
        ollama_vision_model="",
    )
    assert photo_entry_configured(settings) is False


def test_analysis_provider_default():
    """Default provider is 'ollama'."""
    settings = Settings(session_secret="test")
    assert analysis_provider(settings) == "ollama"


def test_analysis_provider_openai():
    """OpenAI provider is recognized."""
    settings = Settings(
        session_secret="test",
        analysis_provider="openai",
    )
    assert analysis_provider(settings) == "openai"


def test_analysis_provider_case_insensitive():
    """Provider name is case-insensitive."""
    settings = Settings(
        session_secret="test",
        analysis_provider="OPENAI",
    )
    assert analysis_provider(settings) == "openai"


if __name__ == "__main__":
    test_photo_entry_configured_ollama()
    test_photo_entry_configured_openai()
    test_photo_entry_configured_openai_missing_key()
    test_photo_entry_configured_openai_missing_model()
    test_photo_entry_configured_openai_missing_url()
    test_photo_entry_configured_unknown_provider()
    test_photo_entry_configured_ollama_missing_fields()
    test_analysis_provider_default()
    test_analysis_provider_openai()
    test_analysis_provider_case_insensitive()
    print("All W2 configuration tests passed!")
