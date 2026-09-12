# C1: Context

## Overview

This document describes the context of the Golf League application, including its stakeholders, environment, and constraints.

## Stakeholders

- **Administrators**: Manage courses, seasons, and users
- **Players**: Participate in tournaments and view their statistics
- **System**: Manages authentication, database operations, and business logic

## Environment

The application will run in a containerized environment with:
- Python 3.11+ runtime
- SQLite database
- FastAPI web framework
- Docker containerization

## Constraints

1. Single app container with SQLite database
2. Three architectural layers: API, services, and domain
3. No scoring/provider modules in MVP
4. All imports from domain layer must not import sqlalchemy, fastapi, or golf_league.models directly
5. Passwords are never stored in plaintext
6. Tokens and secrets are not logged
