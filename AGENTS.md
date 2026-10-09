# Repo context: Golf League

This file is what the global agents (architect, architect-critic, engineer, tester, pr-reviewer)
read to get this repo's facts. The workspace-level `../AGENTS.md` covers the boundary with the
orchestrator repo next door.

## Stack
- Python 3.12+ (local venv is 3.13), uv + make for dependency/task management (never
  `pip install`; never hand-edit `uv.lock`).
- FastAPI/Starlette, SQLAlchemy 2.x, Alembic, Jinja2, SQLite.
- Domain layer (`golf_league/domain/`) must stay pure -- no SQLAlchemy/FastAPI/model imports.
  `services/` is session-taking; `routers/` is HTTP-only. `tests/test_architecture.py` enforces
  domain purity, and that `services/` never imports FastAPI, by AST scan, including dotted
  submodules (`sqlalchemy.orm` counts as `sqlalchemy`). Nothing checks `routers/` automatically.
- Entry point is `golf_league/app.py:create_app(settings=None)`; module-level `app` is what uvicorn
  serves. The `golf_league` console script in `pyproject.toml` points at a `main` that does not
  exist -- run uvicorn directly.

## Commands
- Install: `uv sync --frozen --extra dev`
- Test: `uv run pytest <paths> -q` for the files you touched (serial, no xdist). Full suite:
  `uv run pytest -n auto -q` (~637 tests, ~40s with `pytest-xdist`; ~90s serial).
- Full gate: `make check` = `lint test-with-cov security dependency-check` (the coverage run is the
  only full-suite run, parallel via `-n auto`); requires >=80% branch coverage on `golf_league`.
  This is exactly what CI runs on `main` and `reset/**`. During development run only the tests for
  what you changed; leave the full gate to `gl fresh-check` or CI.
- Run locally: `SESSION_SECRET=dev uv run uvicorn golf_league.app:app --reload`
- Only `ruff check` is wired in. Do not run `ruff format` -- it would rewrite ~63 files.
  Formatting repairs are explicit edits, never a hidden side effect of `make check`.
- `Settings.session_secret` has no default; constructing `Settings()` without `SESSION_SECRET`
  raises. Tests pass it explicitly -- do not add a fallback.
- The test client needs `httpx2` (not `httpx`); it is a `dev` extra and must stay in `uv.lock`.

## Paths
- Repository: `/Users/aaron/Development/apps/golf/golf_league`
- Task/plan tracking: Obsidian vault `/Users/aaron/Obsidian`, project folder
  `01 Projects/Golf League/`
  - Execution guide: `01 Projects/Golf League/Tasks/00-Execution-Guide.md`
  - Rulings (the current answer to every decision, versioned): `Plan/Rulings.md`
  - MVP plan: `01 Projects/Golf League/Plan/MVP Build Plan.md`
  - Background: `01 Projects/Golf League/Plan/Planning.md`
- Self-contained: this repository and `Plan/Rulings.md` are the only authorities. A spec or review
  that points at another repository is a defect -- the contract gets written out inline instead.
- Stale, ignore: `.clinerules` describes the Cline/CrewAI workflow removed on 2026-09-14.

## Map
- `tests/milestones/M6/test_M6_exit_criteria.py` is the aggregate player-view gate for
  regular, substitute, self-match, unused and unlinked personas, makeup navigation, and privacy.
- `.agents/codemap.md` is the committed source-file manifest and Python symbol index.
- Regenerate with `uv run python scripts/generate_codemap.py` after adding, moving, renaming,
  or deleting source files. The stdlib generator indexes existing tracked and nonignored source
  files, excludes private data, and does not claim to describe runtime call paths.
- Admin invitation lifecycle routes are in `golf_league/routers/admin_invites.py`; shared join
  and set-password routes are in `golf_league/routers/join.py`. Their invite and credential
  operations are in `golf_league/services/invites.py`, with password-reset completion in
  `golf_league/services/auth.py`.
- Player home and contact entry points are `golf_league/routers/home.py` and
  `golf_league/routers/player_schedule.py`; projections are in
  `golf_league/services/player_schedule.py`, with markup in `templates/player/`.
- Managed configuration (the R-DEPLOYMENT eight-key allowlist, the atomic managed `.env`
  file and its validation) is `golf_league/managed_config.py`; `Settings` in
  `golf_league/config.py` reads the eight managed keys from that file above the environment,
  and the deploy-time keys (`DEPLOY_FILE_KEYS`, incl. `SESSION_SECRET`) from it below the
  environment; `bootstrap_admin` reads `ADMIN_*` the same way. The admin form is
  `golf_league/routers/admin_config.py` with `templates/admin/config.html`; behaviour is
  documented in `docs/configuration.md`. Tests point `MANAGED_ENV_PATH` at a temporary file
  through an autouse fixture in `tests/conftest.py`.
- Deployment templates are `deploy/compose.example.yml` and `deploy/swag.example.conf`
  (placeholders only); `docs/deployment.md` and `docs/backup-restore.md` describe the Unraid/SWAG
  shape, trusted-proxy client IP, startup order and SQLite backup. `tests/test_deployment_artifacts.py`
  guards their safety properties.
- `tests/test_accessibility.py` is the static (non-browser) audit of rendered pages and
  `static/app.css`: labels, skip link, button names, escaping, 44px targets, one breakpoint.

## Invariants
- Server-side validation of IDs and season/course relationships; SQLite FKs, uniqueness
  constraints, atomic transactions for multi-record changes.
- Migrations: forward-only, data-preserving, tested against a fresh DB and prior-version upgrades.
  Single chain in `migrations/versions/` (`496c039e7ac0` -> ... -> `b8d4c0e2f671`); check
  `down_revision` before adding one.
- `lifespan` in `app.py` deliberately swallows engine/migration failures so the process stays up
  and `/readyz` can honestly answer 503. `/healthz` is process-only. Do not turn this into a crash.
  Startup order: `upgrade_to_head` -> `bootstrap_admin` (no-op unless `users` is empty) ->
  `seed_wyandot` unless `seed_course=False`.
- Calendar dates as `Date`; event timestamps UTC-aware. Preserve signed handicaps and zero; blank
  means missing, not zero.
- Match generation: the internal generator (no public partial-write endpoint) is separate from the
  atomic public generate-and-snapshot action -- never expose a mutation that can succeed with only
  half the operation complete.
- Identity: Argon2 password hashing, signed cookies, `session_version`, hashed single-use tokens,
  `MAX_USERS=150`, an authorization ladder. Every POST (including anonymous forms) requires CSRF.
  GETs never mutate state.
- Admin-only: roster-wide contacts, import staging, raw imported rows. A verified player may view
  the contact details of their direct opponent in a scheduled generated player match, through a
  match-scoped lookup that does not expose arbitrary golfers. Use synthetic fixtures in tests --
  never real roster data, secrets, tokens, or complete token URLs in code, logs, or evidence. Never
  open or modify real `.env` files.
- Runtime: single non-root container, SQLite persistence under `/data`, startup migrations,
  redacted stdout/stderr logs.
- Out of scope unless a work item explicitly adds it: scoring, rounds, results, standings, scoring
  rulesets, automatic team formation/pairings, handicap recomputation, AI/provider modules. Do not
  restore PDF parsing, statistics, public roster, export, undo/redo, or load balancing.
- Product decisions (tee mapping, handicap units, and the rest) live in exactly one place:
  `Plan/Rulings.md` in the vault, one versioned ruling per topic. The guide's older Q1-Q9/D1-D9
  table and the frozen Decision Register are history, not answers. Treat a "suggested default"
  anywhere else as unconfirmed.

## PR facts
- Hosting: `git@github.com:adhatcher-org/golf_league.git`. Work lands on `main` through a GitHub
  PR from a task branch (`codex/gl-12-import-apply`, `reset/m0-rebuild`); CI runs `make check` on
  both `main` and `reset/**`.
- Orchestrator-produced commits are titled `GL-NN candidate: attempt N` and are made by
  `gl commit`, never by hand.

## Pre-PR review is mandatory -- before anything reaches GitHub
This applies to every agent and runtime (Claude Code, Codex, opencode, or any other). A request to
"push", "open a PR", or "create a pull request" means: review first, then push and open the PR.
- Before `git push` of a branch meant for a PR, and before `gh pr create`, run the pre-PR review
  defined in `/Users/aaron/.agents/agents/pr-reviewer.md` against the committed HEAD. Read that
  file in full first; it is the review contract.
- Run the review as a separate reviewer, not as the agent that wrote the code: opencode's
  `pr-reviewer` subagent, a Codex subagent, or a Claude Code subagent told to read
  `pr-reviewer.md` first. Only if the runtime cannot launch a subagent may the primary agent do the
  review itself, and it must say so in its report and in the PR body.
- The review includes `make check` on the reviewed commit. Any actionable finding means nothing is
  pushed and no PR is opened: report the findings, fix them, and review again.
- A review is bound to one commit SHA. If HEAD changes after the review (a fix, an amend, a
  rebase), the review is void; review the new HEAD before pushing.
- The PR body must include a "Pre-PR review" section with the reviewed SHA, who reviewed it
  (which subagent/model), the commands run, and the findings summary.
- This is enforced, not just asked for: Claude Code, Codex and opencode all block direct
  `git push` and `gh pr create`. The only way through is `~/.agents/bin/pr-gate` -- the reviewer
  runs `pr-gate record` after a clean review, then `pr-gate push` and `pr-gate pr-create`, which
  refuse any commit without a recorded PASS. Do not try to route around it.
- Agents cannot skip the review. If the user wants to push without one, they push from their own
  terminal.

## Verification methods available
- Unit/integration tests via pytest -- always available.
- Fixtures in `tests/conftest.py`: `session` (in-memory, FK pragma on, rolled back per test),
  `client` (temp-file DB with Wyandot seeded), `empty_client` (`seed_course=False`),
  `admin_client`/`empty_admin_client` (insert a verified admin, then log in through the real
  `/login` form). CSRF is real in tests -- every POST needs a token scraped from the rendered
  form; there is no bypass.
- `tests/milestones/<M>/` holds milestone exit-criteria tests, not ordinary unit tests.
- `tests/milestones/M0/docker_health_demo.sh` is deliberately excluded from pytest collection.
  Run it by hand with a local untracked `.env`; it does a real compose build/up/restart cycle.
- Browser automation -- not yet wired up for this repo; `tester` should report browser-journey
  items as "not run -- no browser automation capability configured" until this changes.
- Bootstrap exception: the earliest scaffold-and-tooling work items can close on a documentation-only
  check before test tooling exists; once tooling exists, revisit for full verification.
