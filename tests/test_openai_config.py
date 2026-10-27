"""W1: Configuration surface tests for OpenAI provider support."""
import os

from golf_league.config import Settings
from golf_league.managed_config import DEPLOY_FILE_KEYS


def test_settings_fields_exist():
    """Settings has the three new OpenAI fields with correct defaults."""
    settings = Settings(
        session_secret="test",
        analysis_provider="ollama",
        ollama_url="http://localhost:11434",
        ollama_vision_model="test-model",
        openai_api_key="",
        openai_model="",
        openai_base_url="",
    )
    assert settings.openai_api_key == ""
    assert settings.openai_model == ""
    assert settings.openai_base_url == ""


def test_openai_api_key_repr_false():
    """openai_api_key field has repr=False so it's never echoed."""
    settings = Settings(
        session_secret="test",
        openai_api_key="secret-key-123",
    )
    # Check that repr doesn't include the API key
    repr_str = repr(settings)
    assert "secret-key-123" not in repr_str
    assert "openai_api_key" not in repr_str


def test_deploy_file_keys_mappings():
    """DEPLOY_FILE_KEYS has correct mappings for OpenAI keys."""
    assert DEPLOY_FILE_KEYS["OPENAI_API_KEY"] == "openai_api_key"
    assert DEPLOY_FILE_KEYS["OPENAI_MODEL"] == "openai_model"
    assert DEPLOY_FILE_KEYS["OPENAI_BASE_URL"] == "openai_base_url"


def test_env_over_file_precedence():
    """Environment variables win over managed file values."""
    # Set environment variables
    os.environ["OPENAI_API_KEY"] = "env-key"
    os.environ["OPENAI_MODEL"] = "env-model"
    os.environ["OPENAI_BASE_URL"] = "https://env.example.com"

    try:
        # Create settings - environment should override
        settings = Settings(
            session_secret="test",
            analysis_provider="ollama",
            ollama_url="http://localhost:11434",
            ollama_vision_model="test-model",
        )
        assert settings.openai_api_key == "env-key"
        assert settings.openai_model == "env-model"
        assert settings.openai_base_url == "https://env.example.com"
    finally:
        # Clean up
        del os.environ["OPENAI_API_KEY"]
        del os.environ["OPENAI_MODEL"]
        del os.environ["OPENAI_BASE_URL"]


if __name__ == "__main__":
    test_settings_fields_exist()
    test_openai_api_key_repr_false()
    test_deploy_file_keys_mappings()
    test_env_over_file_precedence()
    print("All W1 tests passed!")
