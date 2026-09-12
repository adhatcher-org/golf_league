# 1. Current Architecture Baseline

## Status

This document describes the baseline architecture for the Golf League application.

## Context

The Golf League application requires a modern, scalable web application that can handle:
- User authentication and authorization
- Course management 
- Roster management
- Season management
- Tournament scheduling

## Decision

We will implement the application using:
- Python 3.11+ with FastAPI for the web framework
- SQLite for the database (with Alembic for migrations)
- uv for dependency management
- Ruff for linting and formatting
- pytest for testing
- Docker for containerization

## Consequences

This architecture provides a solid foundation for building a scalable web application while maintaining simplicity for the MVP.
