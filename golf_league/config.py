from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/golf_league.db"
    session_secret: str
    max_users: int = 150
    debug: bool = False
    seed_course: bool = True


def get_settings() -> Settings:
    return Settings()
