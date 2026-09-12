# Implementation Decisions

## Overview

This document records key implementation decisions made for the Golf League application.

## Database Design

- Use SQLite as the primary database
- Use Alembic for database migrations
- All database operations will be wrapped in transactions
- Foreign key constraints will be enforced at the database level

## Authentication and Authorization

- Use signed cookies with session version checking
- Passwords will be hashed using Argon2
- Session management will follow security best practices
- User roles will be managed with explicit admin flags

## Security

- All passwords will be stored as hashes, never in plaintext
- CSRF protection will be implemented for all mutations
- Tokens and secrets will not be logged
- Session cookies will be secure and HttpOnly

## Testing

- Unit tests will be written using pytest
- Test coverage will be maintained at 80%+ branch coverage
- Integration tests will cover key user flows
- No real SMTP or external providers will be contacted during testing

## Architecture

- Single app container with SQLite database
- Three layers: API, services, and domain
- No scoring/provider modules in MVP
- All imports from domain layer must not import sqlalchemy, fastapi, or golf_league.models directly
