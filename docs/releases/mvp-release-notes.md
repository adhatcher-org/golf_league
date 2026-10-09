# MVP release notes (draft)

Tag: assigned by the publish workflow when the release commit lands on `main`
(`YYYY.MM.DD-N`); record the tag and image digest here when known. Package version in
`pyproject.toml`: 0.1.0.

## What the MVP does

- **Identity:** first-admin bootstrap, login/logout, roster-restricted registration with email
  verification, password reset, 150-account cap, CSRF on every form, rate-limited login and
  registration.
- **Shared league link:** admin-issued, digest-only invite links (create, show once, rotate,
  revoke) that send a personal set-password link only to active roster addresses, with
  per-email, per-IP, per-invite and global send quotas and a neutral response.
- **Roster:** CSV import with staging, row correction/exclusion, atomic idempotent apply,
  90-day staging retention and purge; single-golfer editing; privacy-safe player roster.
- **Course:** Wyandot seeded (two tees, six ratings, 18 holes, 36 yardages); full editing.
- **Seasons and teams:** season configuration, golfer selection with derived roles, per-season
  tee and seed overrides, manual teams and positions.
- **Schedule and matchups:** typed printed weeks, date-shift and nine-change previews, cancelled
  weeks kept, rain-date activation, explicit team pairings generating player matches and
  week handicap snapshots, self-matches, durable substitutions.
- **Player views:** home with upcoming matches, My Schedule, weekly matchup pages, scoped
  opponent contact details.
- **Operations:** admin-managed mail/log settings in a 0600 data-dir file, SMTP relay with
  verifying TLS, absolute links from the configured origin, `Secure` cookies on https,
  redacted logs, health/readiness endpoints, startup migrations, SWAG/Unraid templates and
  backup/restore procedure.

## Not in the MVP

Scoring, rounds, results, standings, statistics, automatic team drafting or pairing, handicap
recomputation, export, public roster, undo/redo, PDF parsing, AI/provider integrations.

## Upgrade notes

- First install: follow `docs/deployment-runbook.md`.
- `SESSION_SECRET` may live in the data-directory `.env` (mode 0600); the environment wins.
- Changing `SESSION_SECRET` signs everyone out.
