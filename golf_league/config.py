import os
import string
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from golf_league.managed_config import (
    DEFAULT_MANAGED_ENV_PATH,
    MANAGED_KEYS_BY_NAME,
    read_managed_values,
)


class _ManagedFileSource(PydanticBaseSettingsSource):
    """The admin-managed file: allowlisted keys only, above the environment."""

    def __init__(self, settings_cls: type[BaseSettings], path: str):
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values = read_managed_values(Path(self._path))
        return {MANAGED_KEYS_BY_NAME[name].field: value for name, value in values.items()}


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/golf_league.db"
    session_secret: str
    max_users: int = 150
    external_base_url: str = "https://golfleague.aaronhatcher.com"
    debug: bool = False
    email_verification_required: bool = True
    seed_course: bool = True
    league_name_template: str = "St. Paul {season} Fall Golf League"

    # Managed configuration (R-DEPLOYMENT). These eight may be edited from
    # /admin/config; each takes effect at the next restart.
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = Field("", repr=False)
    smtp_from_email: str = "golfleague@aaronhatcher.com"
    smtp_from_name: str = "St. Paul Golf League"
    smtp_tls_mode: Literal["starttls", "ssl", "none"] = "starttls"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # Environment-only: where the managed file lives. Never read from that file.
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Explicit arguments, then the managed file, then the environment."""
        init_kwargs = getattr(init_settings, "init_kwargs", {})
        path = (
            init_kwargs.get("managed_env_path")
            or os.environ.get("MANAGED_ENV_PATH")
            or DEFAULT_MANAGED_ENV_PATH
        )
        return (
            init_settings,
            _ManagedFileSource(settings_cls, str(path)),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    @field_validator("smtp_tls_mode", mode="before")
    @classmethod
    def tls_mode_is_case_insensitive(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def log_level_is_case_insensitive(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("external_base_url")
    @classmethod
    def external_origin_must_be_absolute(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("external_base_url must be an absolute HTTP(S) origin.") from exc
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or "?" in value or "#" in value
            or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value
            or (port is not None and port < 1)
        ):
            raise ValueError("external_base_url must be an absolute HTTP(S) origin.")
        return value.rstrip("/")

    @field_validator("league_name_template")
    @classmethod
    def league_name_template_must_support_season(cls, value: str) -> str:
        """Require an ordinary format string that actually uses ``season``."""
        try:
            fields = [field for _, field, _, _ in string.Formatter().parse(value)]
            value.format(season=2026)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("league_name_template must support {season}.") from exc
        if not any(field and field.split(".", 1)[0].split("[", 1)[0] == "season" for field in fields):
            raise ValueError("league_name_template must include {season}.")
        return value


def get_settings() -> Settings:
    return Settings()
