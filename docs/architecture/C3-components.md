# C3: Components (as built, M7)

Three layers inside `golf_league/`, with the boundaries enforced by `tests/test_architecture.py`:

- `domain/` — pure rules: no SQLAlchemy, FastAPI or models imports.
- `services/` — take a SQLAlchemy `Session`; never import FastAPI. Helpers that must join a
  larger transaction only flush; the operation owner commits.
- `routers/` — the only HTTP layer: authentication dependencies, CSRF, form parsing, status
  codes (R-FORMS), templates.

Cross-cutting modules at the package root: `app.py` (factory, lifespan, navigation middleware,
health), `config.py` (`Settings`), `managed_config.py` (managed `.env` allowlist, reader,
atomic writer), `security.py` (CSRF, cookie `Secure` rule, the auth dependency ladder),
`database.py`, `migrations.py`, `models.py`, `logging_config.py` (redaction), `admin_config.py`
(first-admin bootstrap).

| Area | Routers | Services | Domain |
|---|---|---|---|
| Identity: login, logout, register, verify, reset | `identity.py` | `auth.py`, `identity.py`, `email.py` | `identity.py`, `tokens.py`, `rate_limit.py` |
| Shared league link and set-password | `admin_invites.py`, `join.py` | `invites.py` | `tokens.py` |
| Roster and CSV import | `admin_roster.py`, `admin_imports.py`, `roster.py` | `roster.py`, `roster_imports.py` | `roster.py`, `roster_import.py` |
| Course, tees, ratings, holes | `admin_courses.py` | `courses.py`, `course_seed.py` | `course.py`, `handicap.py` |
| Seasons, selection, overrides, teams | `admin_seasons.py`, `admin_participants.py`, `admin_teams.py` | `seasons.py`, `season_roster.py`, `participants.py`, `teams.py` | `teams.py`, `roster.py` |
| Weeks, cascades, nine, rain dates | `admin_weeks.py` | `schedule.py` | `schedule.py` |
| Matchups, snapshots, substitutions | `admin_matches.py` | `matchups.py`, `handicaps.py` | `matchups.py` |
| Player views | `home.py`, `player_schedule.py` | `player_schedule.py` | — |
| Managed configuration | `admin_config.py` | — (`managed_config.py`) | — |

Authorization ladder (`security.py`): `get_optional_user` → `require_user` (401) →
`require_verified_user` (403 unverified) → `require_admin` (403 non-admin). Every POST checks
CSRF; admin forms bind the token to the session cookie, anonymous forms to a `csrf_seed`
cookie.
