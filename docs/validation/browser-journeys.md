# Browser and accessibility journeys (GL-61 procedure)

This is the procedure for the GL-61 release journeys. It holds no results. Record
results, with the exact commit, separately; a journey that was not run is recorded as
"not run", never as a pass. Browser automation is not wired into this repository, so these
journeys are run by hand or by an external browser agent.

Widths (R-VIEWPORTS): **390 px** (mobile) and **1280 px** (desktop). Run every journey at
both. The stylesheet has one breakpoint, 720 px.

## 1. Start a local server (synthetic data, fake mail)

```bash
uv sync --frozen --extra dev
uv run python scripts/run_local.py --data-dir /tmp/golf-league-local --demo --port 8000
```

- Everything lives in `--data-dir` (outside the repository): the database, the managed
  settings file (with a generated `SESSION_SECRET`) and `mail/`, where every message the app
  would send is written as a file (verification, reset and set-password links).
- The public origin is `http://127.0.0.1:8000`; open that exact host (not `localhost`) so the
  links in captured mail match.
- First admin: `admin@example.test` / `local-admin-password` (or `ADMIN_EMAIL` /
  `ADMIN_PASSWORD` set in the shell before the first start).
- `--demo` personas, password `local-demo-password`: `player-regular@example.test` (team
  golfer), `player-sub@example.test` (substituted into week 2), `player-unused@example.test`
  (sub with no match), `player-unlinked@example.test` (no golfer link).
- Synthetic CSVs for imports: `tests/fixtures/roster/*.csv`.
- Start from an empty `--data-dir` for the first-admin journey; delete the directory to reset.

For the container check use the image instead: `make build`, then the M0 procedure in
`tests/milestones/M0/docker_health_demo.sh` with a synthetic `SESSION_SECRET`.

## 2. Journeys (each at 390 and 1280 px)

| # | Journey | Pass when |
|---|---|---|
| J1 | First admin bootstrap, login, logout; register a roster address, open the captured verification link | Admin lands on the roster; verification page is neutral; player pages open after verification |
| J2 | CSV import: stage, correct a row, exclude a row, apply, re-import the same file | Review grid usable; apply is atomic; re-import changes nothing |
| J3 | Course: edit course, both tees with three rating scopes, the 18-hole grid with yardages | Values read back exactly |
| J4 | Season: create, select golfers (derived role), optional tee and seed override | Subs stay subs; override shown |
| J5 | Teams: complete assignment, then a position override | Positions persist |
| J6 | Weeks: generate typed weeks; date-shift preview then confirm | Later weeks move by the shown offset |
| J7 | Cancelled week 1 is kept and numbering is unchanged | Week list unchanged |
| J8 | Rain date: activate 9 → 10 with preview, then confirm | Makeup navigation works |
| J9 | Matches: a normal pairing (4 player matches) and a self-match (2, flagged) | Counts and flags as stated |
| J10 | Substitution: blank substitute is rejected; a valid one sticks through regeneration | 422 for blank; sub kept |
| J11 | My Schedule for all four personas | Each sees only own data; no contact data except the scheduled opponent |
| J12 | Shared invite: create, view once, revoke, rotate, hit the send limit, join, captured set-password link, set password | Neutral join response; one-time URL |
| J13 | `/admin/config`: invalid value (error summary), then a valid save ("Restart required") | No secret value shown |
| J14 | Browser back, refresh and double-submit on J2, J6, J9, J12 | No duplicated action |

## 3. Cross-checks

- Authorization matrix on a sample of admin and player URLs: anonymous 401, unverified 403,
  verified non-admin 403 on admin pages, admin 200.
- CSRF: every form posts a token; a POST without it is 403.
- Tampered IDs and cross-season IDs answer 404, never another season's data.
- Player pages and server logs contain no roster contact data, tokens or passwords.
- Token pages (`/join/…`, `/set-password/…`, `/admin/invites/created`) send
  `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
- Long synthetic names (the demo subs) wrap; markup typed into names is shown as text.
- Keyboard only: skip link first, visible focus, every control labelled, menus reachable.
- Touch targets at least 44 px; no page scrolls sideways at 390 px; wide tables scroll inside
  their own region; empty and error states are readable at both widths.
- Server output (the terminal running `run_local.py`, or `docker logs`) has no tracebacks and
  no secrets.

## 4. What this does not prove

A local run with captured mail is not SMTP delivery, and a local server is not the Unraid/SWAG
deployment. Those belong to GL-71's external evidence and GL-72.
