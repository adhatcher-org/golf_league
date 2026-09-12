# C2: Architecture

## Overview

This document describes the overall architecture of the Golf League application.

## Layers

The application follows a three-layer architectural pattern:

1. **API Layer**: Handles HTTP requests and responses, including routing and authentication
2. **Services Layer**: Contains business logic and orchestrates operations between domain objects
3. **Domain Layer**: Represents core business entities and their relationships

## Components

- FastAPI web framework for handling HTTP requests
- SQLite database with Alembic migrations
- Argon2 for password hashing
- Session management with secure cookies
- CSRF protection for all mutations
- Rate limiting for security

## Data Flow

1. HTTP requests are received by the API layer
2. Authentication and authorization checks are performed
3. Business logic is executed in the services layer
4. Domain objects are manipulated to perform operations
5. Results are returned to the client through the API layer
