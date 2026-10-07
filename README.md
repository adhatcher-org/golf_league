# Golf League

Golf League is a web application for managing a golf league's roster, courses, seasons, teams, schedules, and matchups. It is an actively developed MVP and is not yet production-ready.

## Current status

Core league administration and player-facing schedule features are implemented, including roster management and import, course and tee setup, seasons and teams, weekly matchups, schedule snapshots, substitutions, rain-date handling, and player schedule and week views. GL-40 and GL-41, which add player schedule and week-detail views, are under review in [PR #22](https://github.com/adhatcher-org/golf_league/pull/22); they are not yet merged.

Account registration and email verification are configurable for local development. The configured email sender is currently a placeholder and does not send real email. Scoring, results, and standings are outside the current scope.

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) for running without Docker
- Docker with the Compose plugin for running in Docker

The application uses FastAPI, SQLAlchemy, Alembic, Jinja2, and SQLite.

## Run with Docker Compose

From the repository root, provide a session secret and start the service:

```sh
export SESSION_SECRET='replace-with-a-long-random-secret'
docker compose up --build
```

Open <http://localhost:8000>. On a new, empty database, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` before the first startup to create the initial administrator. The database is stored in `./data` and persists across container restarts.

Stop the foreground service with `Ctrl-C`, or run `docker compose down` in another terminal. `docker compose down` preserves the database directory.

## Run without Docker

From the repository root, install the locked dependencies and start Uvicorn:

```sh
uv sync --frozen --extra dev
SESSION_SECRET='dev-only-change-me' uv run uvicorn golf_league.app:app --reload
```

Open <http://127.0.0.1:8000>. The local SQLite database is stored at `./data/golf_league.db`. For first-run administrator bootstrap, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` in the environment before starting the app.

The `golf_league` console command is not configured as a runnable entry point; use the Uvicorn command above. Use `uv` rather than installing dependencies with `pip`.

## Local email-verification setting

`EMAIL_VERIFICATION_REQUIRED` defaults to `true`. For a trusted local development environment only, set it to `false` before starting the app:

```sh
EMAIL_VERIFICATION_REQUIRED=false SESSION_SECRET='dev-only-change-me' uv run uvicorn golf_league.app:app --reload
```

With verification disabled, newly registered accounts are treated as verified and no verification email is sent. Existing unverified accounts can access player pages in this mode. Do not use this setting for a publicly accessible deployment.

## Checks

Run the same project gate used in CI:

```sh
make check
```

## Project layout

- `golf_league/domain/` — framework-independent league rules
- `golf_league/services/` — database-backed operations
- `golf_league/routers/` — HTTP routes
- `templates/` and `static/` — rendered pages and frontend assets
- `migrations/` — Alembic database migrations
