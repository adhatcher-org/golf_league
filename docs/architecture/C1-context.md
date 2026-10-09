# C1: System context (as built, M7)

Golf League is a private web application for one Thursday-evening nine-hole league. It keeps
the roster, the course, seasons, teams, the printed weekly schedule and the weekly player
matchups, and shows each player their own schedule. It does **not** score rounds, keep
standings or statistics, draft teams or pair teams automatically, or recompute handicaps.

## People

- **League administrator** (verified admin): imports and edits the roster, maintains the
  course, configures seasons, teams, weeks and matchups, records substitutions, issues the
  shared league link, edits mail/log settings.
- **Player** (verified user linked to a roster golfer): sees the upcoming matches, their own
  schedule, the weekly matchups, the privacy-safe roster and the contact details of a direct
  opponent in a scheduled match.
- **Anonymous visitor**: can only log in, reset a password, register a roster address, or use
  a shared league link to request a personal set-password link.

## External systems

| System | Relationship |
|---|---|
| SWAG reverse proxy on the owner's Unraid server | Terminates HTTPS for `https://golfleague.aaronhatcher.com` and forwards plain HTTP to the app; the only trusted source of `X-Forwarded-For`/`-Proto`. |
| SMTP relay | Receives verification, password-reset and set-password mail when `SMTP_HOST` is configured; otherwise mail is captured in memory. |
| Players' mail providers | Deliver those messages; SPF/DKIM/DMARC are a deployment prerequisite. |
| GitHub Actions / GHCR | CI runs `make check`; the publish workflow builds the image from `main`. |

## Constraints

Single container, single worker, SQLite on a mounted data directory, startup migrations,
synthetic data only outside production, secrets never logged or displayed. Governing
decisions are the vault's `Plan/Rulings.md`; see also `docs/adr/`.
