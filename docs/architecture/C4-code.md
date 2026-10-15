# C4: Code and data (as built, M9)

The committed symbol index is `.agents/codemap.md` (regenerate with
`uv run python scripts/generate_codemap.py`). This page records the data model and the few
code paths whose shape matters for safety.

## Tables (`golf_league/models.py`, Alembic chain in `migrations/versions/`)

| Group | Tables |
|---|---|
| Identity | `users`, `user_tokens` (hashed single-use verify/reset tokens) |
| Shared link | `league_invite_links` (digest only), `golfer_set_password_tokens`, `invite_rate_windows` (HMAC-keyed quota windows) |
| Roster | `golfers` (one signed `handicap_strokes`, nullable email), `roster_import_batches`, `roster_import_rows` (admin-only, 90-day expiry) |
| Course | `courses`, `tee_sets`, `tee_ratings` (front/back/full scopes), `holes`, `hole_yardages` |
| Season | `seasons`, `season_golfers` (selection), `season_participants` (tee/seed overrides; no role column), `teams`, `team_members` |
| Schedule | `weeks` (`match` / `play_with_team` / `rain_date`, printed index kept) |
| Matchups | `team_matches`, `player_matches` (sub flags, `vs_own_handicap`, `manually_adjusted`), `week_handicaps` (historical snapshots) |
| Scores | `hole_scores` (one row per golfer and hole of a player match, with who first entered it), `match_score_states` (each match's revision and lock), `scorecard_drafts` (a card awaiting review; deleted on submit, discard or after 24 hours, its photo under `<data dir>/scorecards/`) |

There is no role column anywhere (roles derive from team membership), no tee-handicap table,
and no results, standings or statistics table.

## Code paths worth knowing

- **Settings precedence** (`config.Settings.settings_customise_sources`): init args → managed
  keys from the file → environment (empty ignored) → deploy keys from the file → defaults;
  missing `SESSION_SECRET` fails naming the key and file.
- **Managed writes** (`managed_config.write_managed_values`): validate, merge preserving
  unowned lines, temp file + fsync + `os.replace`, mode 0600, directory fsync.
- **Mail**: senders are synchronous and run after the response (`BackgroundTask` / response
  background callbacks); failures log one fixed line. Links use `EXTERNAL_BASE_URL` only.
- **Cookies**: `security.cookie_is_secure` — `Secure` whenever `EXTERNAL_BASE_URL` is https.
- **Serialized writes**: invite admission, set-password/reset completion, schedule and
  matchup writes acquire SQLite write serialization (`BEGIN IMMEDIATE`, or a no-op write when a
  transaction is already open) before reading, then commit once.
- **Logging**: `logging_config.redact` for application lines; the uvicorn access filter
  redacts token path segments and query strings.
