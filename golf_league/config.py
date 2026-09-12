"""Configuration management."""

import os
from pydantic_settings import SettingsBase
from pydantic import Field

class Settings(SettingsBase):
    """Application settings."""
    
    # Database configuration
    database_path: str = Field(default="sqlite:///golf_league.db")
    
    # Session configuration
    session_secret: str = Field(default="change-me-in-production")
    
    # Admin bootstrap inputs
    admin_email: str = Field(default="admin@example.com")
    admin_password: str = Field(default="admin123")
    
    # SMTP configuration (for email notifications)
    smtp_host: str = Field(default="localhost")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    
    # Application settings
    max_users: int = Field(default=150)
    league_name_template: str = Field(default="Golf League {season}")
    
    class Config:
        """Pydantic configuration."""
        env_file = ".env"
        case_sensitive = False

# Load settings
settings = Settings()