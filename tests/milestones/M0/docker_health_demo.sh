#!/usr/bin/env bash
# Manual/CI evidence script for the M0 exit criterion "Docker health demo".
#
# Deliberately NOT part of `make check` and not named test_*.py, so pytest
# never collects it. Run it by hand from the repository root, with
# SESSION_SECRET supplied through an untracked, gitignored `.env` file next
# to docker-compose.yml (never printed by this script or its helpers).
#
# curl is unavailable in some environments this runs in, so all HTTP probing
# goes through tests/milestones/M0/probe_endpoint.py (Python's stdlib
# urllib) instead.
set -euo pipefail
cd "$(dirname "$0")/../../.."

if [ ! -f .env ]; then
  echo "Missing .env with SESSION_SECRET; see docker-compose.yml comments." >&2
  exit 1
fi

echo "== docker compose build =="
docker compose build

echo "== docker compose up -d =="
docker compose up -d

echo "== waiting for /healthz =="
python tests/milestones/M0/probe_endpoint.py http://localhost:8000/healthz 200 90

echo "== waiting for /readyz =="
python tests/milestones/M0/probe_endpoint.py http://localhost:8000/readyz 200 90

echo "== docker compose ps (expect healthy) =="
docker compose ps

echo "== alembic_version before restart =="
python tests/milestones/M0/read_alembic_version.py data/golf_league.db

echo "== docker compose restart =="
docker compose restart

echo "== waiting for /readyz again =="
python tests/milestones/M0/probe_endpoint.py http://localhost:8000/readyz 200 90

echo "== alembic_version after restart (must match) =="
python tests/milestones/M0/read_alembic_version.py data/golf_league.db

echo "== docker compose down =="
docker compose down

echo "== done =="
