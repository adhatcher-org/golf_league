# C4: Code Structure

## Overview

This document describes the code structure and organization of the Golf League application.

## Directory Structure

```
golf_league/
├── __init__.py
├── app.py
├── config.py
├── admin_config.py
├── database.py
├── models.py
├── migrations.py
├── logging_config.py
├── domain/
├── services/
└── routers/
templates/
└── base.html
static/
├── app.css
└── app.js
tests/
├── conftest.py
└── test_architecture.py
```

## Key Files

1. **app.py**: Main application entry point that creates the FastAPI app
2. **config.py**: Configuration management including database path, session secret, and admin bootstrap inputs
3. **database.py**: Database connection and initialization logic
4. **models.py**: Database models and their relationships
5. **migrations.py**: Migration handling for database schema changes
6. **logging_config.py**: Logging configuration

## Code Organization

- All imports from domain layer must not import sqlalchemy, fastapi, or golf_league.models directly
- Services layer contains business logic separate from API layer
- Routers handle HTTP routing and request/response processing
- Templates contain HTML structure with proper escaping of user content
- Static assets include CSS and JavaScript for the frontend

## Testing

- Tests are organized under the `tests/` directory
- `test_architecture.py` specifically tests that forbidden imports are not present
- All tests use isolated databases to ensure test isolation
- Fake mail transport is used during testing instead of real SMTP
