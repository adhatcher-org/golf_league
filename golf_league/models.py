"""Database models."""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    """User model."""
    
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(254), unique=True, index=True)
    email = Column(String(254), unique=True, index=True)
    display_name = Column(String(254))
    password_hash = Column(Text)
    email_verified_at = Column(DateTime)
    is_admin = Column(Boolean, default=False)
    session_version = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)