# 1. Architecture baseline

## Status

Accepted, as built through M7 (2026-10-08). Replaces the earlier skeleton text of this ADR,
which predated the code and named features the MVP excludes.

## Context

One small league needs a private, low-maintenance web app for its roster, course, seasons,
teams, weekly schedule and matchups, run by one administrator on a home Unraid server behind
an existing SWAG proxy. Scoring, standings, statistics, automatic drafting/pairing, handicap
recomputation and AI/provider integrations are explicitly out of scope for the MVP.

## Decision

- Python 3.12+, FastAPI/Starlette, SQLAlchemy 2, Alembic, Jinja2 server-rendered HTML, SQLite;
  dependencies managed by `uv` from a committed lock file.
- Three layers in one package: pure `domain/`, session-taking `services/`, HTTP-only
  `routers/`, with the boundaries checked by `tests/test_architecture.py`.
- One container, one uvicorn worker, SQLite and the managed settings file on a mounted data
  directory; forward-only migrations at startup; `/healthz` and `/readyz`.
- Security baseline: Argon2 passwords, signed session cookies with `session_version`, hashed
  single-use tokens, CSRF on every POST, a fixed 150-account cap, `Secure` cookies and links
  derived from the configured `EXTERNAL_BASE_URL`, redacted logs.
- Quality gate: `make check` (Ruff, full pytest suite with at least 80% branch coverage,
  `uv pip check`, outdated report) in CI on `main` and `reset/**`.

## Consequences

- Simple to run and back up (one SQLite file plus one settings file), but limited to a single
  worker: invite receipts and rate limiters are process-local.
- Product decisions live in the vault's `Plan/Rulings.md`; ADRs here record the technical
  shape (0002 handicaps, 0003 shared invitations, 0004 configuration and deployment).
