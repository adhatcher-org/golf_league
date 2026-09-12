# C3: Components

## Overview

This document describes the components that make up the Golf League application.

## Main Components

1. **Authentication System**: Handles user login, registration, and session management
2. **Database Layer**: Manages SQLite database operations and migrations
3. **User Management**: Handles user creation, modification, and deletion
4. **Course Management**: Manages golf course information
5. **Season Management**: Handles season scheduling and tournament organization

## Component Interactions

- Authentication system interacts with the database to validate users
- User management component communicates with the database layer for persistence
- Course and season management components use shared domain objects
- All components follow the three-layer architecture pattern

## Dependencies

- Database layer is a core dependency for all other components
- Authentication system depends on the database and cryptographic libraries
- All components must not import sqlalchemy, fastapi, or golf_league.models directly from the domain layer
