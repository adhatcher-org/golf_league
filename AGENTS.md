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
  serves. `golf_league.app:main` (also the `golf_league` console script, and the container's
  command) starts uvicorn on 0.0.0.0:8000 and takes `FORWARDED_ALLOW_IPS` from the environment
  or, failing that, the managed file. For development run uvicorn directly.

## Commands
- Install: `uv sync --frozen --extra dev`
- Test: `uv run pytest <paths> -q` for the files you touched (serial, no xdist). Full suite:
  `uv run pytest -n auto -q` (~1,540 tests, ~90s with `pytest-xdist`).
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
  `tests/milestones/M9/test_M9_exit_criteria.py` is the score-entry gate (R-SCORES version 2):
  player entry and change, cards, the admin week sheet, locks, card privacy and the rain-date
  guard, all through the real routes.
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
- Scorecard validation and match grouping are in `golf_league/domain/scorecard.py`;
  `golf_league/services/scores.py` checks week eligibility and atomically writes hole scores,
  and owns updating, locking, unlocking and clearing a match (`match_score_states` holds each
  match's revision and lock).
- The admin week sheet, one-match page, lock, unlock, "Lock all" and clear are
  `golf_league/routers/admin_week_scores.py` with `templates/admin/scorecards/scores.html`,
  `match.html` and `clear.html`; the shared score-grid partials are in `templates/scores/`, and
  `golf_league/services/match_entry.py` names the score fields and reads a posted form.
- Player score pages (R-SCORES) are `golf_league/routers/player_scores.py` with
  `templates/player/scores/`: the read-only week's scores (`/weeks/{week}/scores`) for any
  verified user, and the entry page and one-match form for a player, who always acts as
  `Actor(user.id, False, user.golfer_id)`. Its `entry_links` give My Schedule and the week page
  their "Enter scores" / "Update scores" / "View scores" links; `home_entry_prompt` in
  `golf_league/services/player_schedule.py` is home's "Week N: Enter scores".
- A linked player's read-only round history is in
  `golf_league/routers/player_rounds.py`, `golf_league/services/player_rounds.py`,
  and `templates/player/rounds.html`; it reads saved hole scores and weekly
  handicap snapshots without changing either.
- Handicap trend pages are in `golf_league/routers/handicaps.py` and
  `templates/player/handicap_trend.html` / `templates/admin/handicap_trend.html`.
  `golf_league/services/handicaps.py:get_handicap_trend` reads stored weekly
  snapshots for one golfer, optionally within one season; it never recomputes them.
- Player substitute requests are in `golf_league/routers/substitute_requests.py`
  and `golf_league/services/substitute_requests.py`; only an admin with the emailed
  single-use token may fulfill a request through the existing substitution core.
- Weekly RSVPs and admin reminders are in `golf_league/routers/rsvp.py` and
  `golf_league/services/rsvp.py`; `week_rsvps` stores only golfer, week, status,
  and timestamps. The admin week sheet reads headcount from that table.
- The gross/net presentation toggle on season and week Results pages reads
  saved hole scores and signed weekly snapshots through `services/scores.py`.
- Scorecard photo entry (R-SCORES, R-OCR-PROVIDER): the admin pages are
  `golf_league/routers/admin_scorecards.py` with `templates/admin/scorecards/` and the status
  polling script `static/scorecard-status.js`. The five draft pages (page, status, photo, submit,
  discard) are shared through `golf_league/routers/scorecard_flow.py`, parameterised by a `Flow`,
  with the bodies in `templates/scorecards/_review.html`, `_status.html` and `_failed.html`;
  `open_draft` there decides who may open a draft. A player's own card is
  `golf_league/routers/player_scorecards.py` (`/weeks/{week}/scorecards` to start one, then
  `/scorecards/{draft}`) with `templates/player/scorecards/`: only its creator can open it, photo
  uploads are limited by `app.state.scorecard_upload_limiter` and the waiting queue, and every
  photo is prepared off the event loop, one at a time, under `app.state.scorecard_prepare_lock`.
  `golf_league/services/scorecard_image.py` turns an upload into a clean JPEG; `golf_league/services/scorecard_drafts.py` holds the short-lived
  `scorecard_drafts` rows and photos (under `<data dir>/scorecards/`) and runs the read on one
  background worker; `golf_league/services/scorecard_vision.py` is the only provider module (the
  Ollama client and the five deploy-time keys); `golf_league/services/scorecard_review.py` builds
  the review form and reads a submitted one, using the pure name matching and card checks in
  `golf_league/domain/scorecard_review.py`. Tests replace `app.state.vision_provider`,
  `app.state.scorecard_executor` and `app.state.scorecard_utc_now`.
- Managed configuration (the R-DEPLOYMENT eight-key allowlist, the atomic managed `.env`
  file and its validation) is `golf_league/managed_config.py`; `Settings` in
  `golf_league/config.py` reads the eight managed keys from that file above the environment,
  and the deploy-time keys (`DEPLOY_FILE_KEYS`, incl. `SESSION_SECRET`) from it below the
  environment; `bootstrap_admin` reads `ADMIN_*` the same way. The admin form is
  `golf_league/routers/admin_config.py` with `templates/admin/config.html`; behaviour is
  documented in `docs/configuration.md`. Tests point `MANAGED_ENV_PATH` at a temporary file
  through an autouse fixture in `tests/conftest.py`.
- Deployment templates are `deploy/compose.example.yml` and `deploy/swag.example.conf`
  (placeholders only), with `deploy/example.env` supplying placeholder values so
  `docker compose config` can validate the template; `docs/deployment.md` and `docs/backup-restore.md` describe the Unraid/SWAG
  shape, trusted-proxy client IP, startup order and SQLite backup. `tests/test_deployment_artifacts.py`
  guards their safety properties.
- `README.md` covers setup, running and the admin/player journeys. Release preparation:
  `docs/deployment-runbook.md` (GL-72 steps, not executed), `docs/release-checklist.md`,
  `docs/releases/mvp-release-notes.md` and `docs/releases/m9-release-notes.md`; as-built
  architecture in `docs/architecture/C1..C4` and
  `docs/adr/0001..0004`. `scripts/run_local.py` starts a local server with synthetic demo data
  and file-captured mail for browser journeys (`docs/validation/browser-journeys.md`).
- `tests/test_contract_regressions.py` holds the GL-61 contract assertions not covered elsewhere;
  `tests/test_secure_cookies.py` the `Secure`-cookie rule (shared test clients use https).
- `tests/test_accessibility.py` is the static (non-browser) audit of rendered pages and
  `static/app.css`: labels, skip link, button names, escaping, 44px targets, one breakpoint.

## Invariants
- Server-side validation of IDs and season/course relationships; SQLite FKs, uniqueness
  constraints, atomic transactions for multi-record changes.
- Migrations: forward-only, data-preserving, tested against a fresh DB and prior-version upgrades.
  Single chain in `migrations/versions/` (`496c039e7ac0` -> ... -> `e41c7f9a2b36`); check
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
- Hole scores are in scope under R-SCORES: an admin enters, changes, locks, unlocks and clears
  them; a verified player linked to a golfer who plays the week enters and changes their own
  match, and one other match they kept score for, until an admin locks it; every verified user
  sees a week's scores. Every write goes through `services/scores.py`, which refuses a locked
  match and an out-of-date revision. So is the one provider module that reads a scorecard photo
  (`golf_league/services/scorecard_vision.py`) under R-OCR-PROVIDER: no other feature may call a model, no model name appears in code, and no request
  handler calls the provider. Out of scope unless a work item explicitly adds it: rounds, results,
  standings, statistics, scoring rulesets, automatic team formation/pairings, handicap
  recomputation, any other AI/provider module. Do not restore PDF parsing, public roster, export,
  undo/redo, or load balancing.
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
