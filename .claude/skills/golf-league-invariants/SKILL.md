# Golf League Invariants

## Overview

This document defines the core invariants and constraints that must be maintained throughout the Golf League application implementation.

## Core Invariants

1. **Database Integrity**: All database operations are wrapped in transactions, with foreign key constraints enforced at the database level.
2. **Security**: Passwords are never stored in plaintext; tokens and secrets are not logged.
3. **Authorization**: Session cookies are secure and HttpOnly; session version checking prevents replay attacks.
4. **User Management**: 
   - Usernames are limited to 254 characters
   - Emails must be unique and normalized
   - Admin status is explicit and cannot be bypassed by role alone
5. **Data Consistency**: 
   - All IDs are server-resolved and cross-checked for ownership
   - UTC-aware timestamps for events
   - Decimal storage/rounding follows D3 standard
6. **Privacy**: 
   - Synthetic fixtures only; no real roster/contact data in source, test output, logs, metrics, screenshots or git
   - Imports/raw_line remain admin-only private data with retention

## Implementation Contracts

1. **App Creation**: `create_app(settings=None)` constructs app without import-time I/O
2. **Lifespan Management**: App initializes database/migrations before readiness and disposes resources on shutdown
3. **Authentication**: Argon2 hash/verify; normalized email is username; signed secure session cookie with session_version checked
4. **Rate Limiting**: Defined expiry using injected UTC clock; invalid/wrong-purpose/expired/reused tokens fail safely

## Non-Features (MVP Scope)

1. No PDF parsing
2. No bcrypt
3. No scoring/statistics
4. No automatic roles
5. No invented schemas
6. No public roster views
7. No export functionality
8. No undo/redo
9. No load balancing
10. No AI/provider modules
11. No future scoring_ruleset FK

## Testing Constraints

1. All tests must use isolated databases
2. Fake mail transport sends verification/reset link only according to token contract
3. No real SMTP or external providers contacted during automated tests
4. Tests must not contact real SMTP or external providers
5. Test suite uses no SMTP/network
