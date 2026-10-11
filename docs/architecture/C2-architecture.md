# C2: Containers (as built, M7)

```
Browser ──HTTPS──> SWAG (nginx, TLS) ──HTTP :8000──> golf-league container ──> /app/data (host dir)
                                                        │                        ├─ golf_league.db (SQLite)
                                                        │                        ├─ .env (managed settings, 0600)
                                                        └──SMTP (587/STARTTLS)──> relay (optional)
```

## golf-league container

- Image: `python:3.12-slim`, dependencies from `uv.lock` (`uv sync --frozen --no-dev`),
  non-root `appuser` (1000:1000), `uvicorn golf_league.app:app` on port 8000, one worker.
- Health: `/healthz` (process) and `/readyz` (startup complete and database answering); the
  image healthcheck probes `/readyz`.
- Startup (`lifespan` in `golf_league/app.py`): resolve settings (environment plus the managed
  file) → apply `LOG_LEVEL` → choose the mail sender → engine → `alembic upgrade head` →
  first-admin bootstrap (empty `users` only) → idempotent Wyandot course seed. Database or
  migration failures keep the process up with `/readyz` 503.
- Server-rendered HTML (Jinja2) with one stylesheet and one small script; no client framework.
- Process-local state: login/registration rate limiters and one-time invite receipts — the
  reason for the single-worker rule.

## Data directory (host bind mount at `/app/data`)

- `golf_league.db`: all business data; Alembic migrations are forward-only.
- `.env`: the managed settings file (`MANAGED_ENV_PATH`). Holds the eight admin-editable keys
  and may hold the deploy-time keys, including `SESSION_SECRET` (R-DEPLOYMENT v3). Secret-
  bearing; part of the private backup.

## Configuration

Deploy-time values from the container environment (fallback: the managed file); eight managed
values edited at `/admin/config` (file above environment), applied on restart. Details:
`docs/configuration.md`.
