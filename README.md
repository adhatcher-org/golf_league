# Golf League

A private web app for one nine-hole Thursday league: roster, course, seasons, teams, the
printed weekly schedule, weekly player matchups, each player's own schedule, and the hole
scores of each week's matches, entered by players and admins and locked by an admin once
checked. It is deliberately small. It does **not** compute results, standings or statistics,
draft teams, pair teams automatically, recompute handicaps, or offer a public roster.

Product decisions live in the project's Rulings (Obsidian vault, `Plan/Rulings.md`); this
repository records the technical shape in `docs/`.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) (never `pip install` into this project).
- Docker, for the container build.

## Set up and check

```bash
uv sync --frozen --extra dev
make check          # ruff, full test suite with >= 80% branch coverage, uv pip check, outdated list
uv run pytest tests/test_roster.py -q   # one file while developing
```

`make check` is exactly what CI runs. Do not run `ruff format`.

## Run locally

Quick start (in-memory mail, http origin so cookies work over plain http):

```bash
SESSION_SECRET=dev EXTERNAL_BASE_URL=http://127.0.0.1:8000 \
MANAGED_ENV_PATH=/tmp/golf-league-dev.env DATABASE_URL=sqlite:////tmp/golf-league-dev.db \
ADMIN_EMAIL=admin@example.test ADMIN_PASSWORD=local-admin-password \
uv run uvicorn golf_league.app:app --reload
```

For manual journeys with synthetic demo data and readable mail, use the helper. It keeps
everything in a scratch directory outside the repository, writes would-be mail to files and
refuses to run with a mail relay configured:

```bash
uv run python scripts/run_local.py --data-dir /tmp/golf-league-local --demo --port 8000
```

It prints the synthetic admin and player-persona logins. See
`docs/validation/browser-journeys.md`.

Container (local): `SESSION_SECRET=dev make run` builds and starts `docker-compose.yml` with
`./data` mounted; `make stop` stops it.

## Configuration

`docs/configuration.md` is the reference. In short:

- Deploy-time: `SESSION_SECRET` (required, no default), `DATABASE_URL`, `EXTERNAL_BASE_URL`,
  `MAX_USERS` (150), `EMAIL_VERIFICATION_REQUIRED`, `ADMIN_EMAIL` / `ADMIN_PASSWORD` (first boot
  only). Environment first, then the managed file.
- Managed at `/admin/config`: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
  `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, `SMTP_TLS_MODE`, `LOG_LEVEL`; saved to the managed file
  (`MANAGED_ENV_PATH`, default `./data/.env`) and applied on restart.
- Cookies are `Secure` whenever `EXTERNAL_BASE_URL` is https.

Deployment: `docs/deployment.md` (Unraid + SWAG), `docs/deployment-runbook.md` (the ordered
release steps), `docs/backup-restore.md`, templates in `deploy/`.

## Using the app

### Administrator

1. **First login.** On first start with an empty database the app creates the admin from
   `ADMIN_EMAIL` / `ADMIN_PASSWORD`, already verified. Log in at `/login`.
2. **Course.** *Manage → Courses*: the Wyandot course and its two tees are seeded; edit the
   course, each tee's front/back/full ratings, and the 18-hole grid with yardages.
3. **Roster.** *Manage → Import roster*: upload the roster CSV (`FirstName, LastName, Tee,
   Handicap, Email, PhoneNumber`), correct or exclude rows on the review grid, then apply.
   Re-importing the same file changes nothing. *Manage → Golfers* edits single golfers.
4. **Season.** *Manage → New season*: year, course, dates, play day, first nine. Then select
   the season's team golfers, set optional per-season tee or seed-handicap overrides, and
   build teams with positions.
5. **Weeks.** Generate the printed weeks (match, play-with-your-team, rain date), shift dates
   with a preview, change a week's nine, cancel a week, or activate a rain date.
6. **Matchups.** For a match week, pair teams (four player matches; a self-match gives two,
   flagged), and record substitutions; regeneration keeps manual substitutions.
7. **Players' accounts.** Players either register with their roster address at `/register`,
   or receive the shared league link: *Manage → League invites* creates, shows once, rotates
   and revokes it.
8. **Scores.** *Manage → Season weeks → Scores* opens a match week's sheet
   (`/admin/weeks/{week}/scores`): every match with its state in words. Type the missing
   scores for several matches and save them at once; open a match to change its saved scores,
   lock or unlock it, or clear them (after a confirmation). "Lock all" locks every fully
   entered match that has not changed since the page was shown and names the rest. A locked
   match refuses every change, an admin's included, until it is unlocked. *Manage → Enter
   scorecard* reads a photographed card for review before anything is saved; a card only adds
   scores, and the list of cards in progress marks one started by a player.
9. **Configuration.** *Manage → Configuration* sets the mail relay and log level; restart the
   container afterwards.

### Player

1. Get an account: `/register` with the address on the roster (then open the emailed
   verification link), or open the shared league link, enter your roster address and follow
   the emailed set-password link.
2. **Home** shows upcoming league matches; **My Schedule** lists your weeks, opponents and
   status (cancelled, rain date, awaiting pairing); each week links to its matchups.
3. From a scheduled match you may view your direct opponent's contact details — nothing else
   of the roster's contact data is visible to players.
4. **Scores.** From the play date onward, My Schedule, the week page and home show "Enter
   scores" for a week you play. Type the nine scores for you and your opponent and save; you,
   your opponent, and whoever first entered a golfer's scores can change them until the
   league admin locks the match ("Update scores", then "View scores" once locked). Instead of
   typing, upload a photo of the card or type a whole card: it can hold your match and one
   other match you kept score for, and you check every name and score before it is saved.
   Every verified player can see a week's scores at "Scores" on the week page.
5. Forgot your password: `/reset`.

## Repository map

- `golf_league/` — `domain/` (pure rules), `services/` (database operations), `routers/`
  (HTTP), plus `app.py`, `config.py`, `managed_config.py`, `security.py`, `models.py`.
- `templates/`, `static/` — server-rendered pages and the single stylesheet.
- `migrations/` — forward-only Alembic chain, applied at startup.
- `tests/` — unit, route and milestone (`tests/milestones/`) tests; synthetic fixtures only.
- `docs/` — configuration, deployment, backup, architecture (C1–C4), ADRs, validation procedure.
- `.agents/codemap.md` — generated source index (`uv run python scripts/generate_codemap.py`).
