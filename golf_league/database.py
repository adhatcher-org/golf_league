"""Database initialization and management."""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .config import settings

# Create database engine
engine = create_engine(settings.database_path, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_database():
    """Initialize the database."""
    # Create tables if they don't exist
    from .models import Base
    
    # Create all tables
    Base.metadata.create_all(bind=engine)
    
    # Verify database is working
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        result.fetchone()

def get_db():
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()