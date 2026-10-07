import string
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/golf_league.db"
    session_secret: str
    max_users: int = 150
    external_base_url: str = "https://golfleague.aaronhatcher.com"
    debug: bool = False
    email_verification_required: bool = True
    seed_course: bool = True
    league_name_template: str = "St. Paul {season} Fall Golf League"

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
