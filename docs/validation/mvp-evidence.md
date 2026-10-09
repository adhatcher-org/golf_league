# GL-61 browser and accessibility journey evidence

Procedure: `docs/validation/browser-journeys.md`. This file records what was actually observed.
A cell is PASS only when the journey's "pass when" criterion was observed; anything not
observed is NOT RUN or listed under "What this does not prove".

| Item | Value |
|---|---|
| Commit under test | `23066ad94853c0033a36852ee700c4aece9d2a9a` (branch `codex/m7-release-prep`, clean tree) |
| Date | 2026-10-08 |
| Environment | Local `scripts/run_local.py` servers (uvicorn, SQLite in `/tmp`), built-in browser pane with viewport emulation (390x844 and 1280x800), synthetic data only, mail captured to files in `<data-dir>/mail/` |
| Not this environment | NOT SMTP delivery. NOT the Unraid/SWAG deployment. NOT the Docker image. NOT a physical phone. |
| Servers | `--demo` on port 8000 (J2-J14); fresh, non-demo data dir on port 8011 for J1 (port 8001 was occupied by another local process, so 8011 was used). Servers stopped and `/tmp/golf-league-local*` removed afterwards. |
| Credentials | Only the synthetic ones from the procedure. No real `.env` was opened. |

## How the journeys were driven (read this before trusting a PASS)

- Pages were loaded and clicked in the built-in browser. Many form fills and submissions used
  `form.requestSubmit()` from the page's own JavaScript (a real browser form post with a real
  CSRF token) rather than typed keystrokes. Some confirm buttons were clicked with real mouse
  events (marked in the notes).
- The CSV upload used a `File` built in script and assigned to the file input; the OS file
  chooser was not used. The CSVs were synthetic and derived from `tests/fixtures/roster/*.csv`
  plus extra synthetic rows (one with markup in the first name and a very long surname).
- Double-submit and replay checks used two parallel `fetch` POSTs with the form's own token,
  not a physical double click.
- Because most journeys change shared state, a journey was run end to end at one width and its
  mutation repeated on a different record at the other width where possible (the notes say
  which). Layout, overflow and target-size checks were run on every page at both widths.
- Login is throttled (5 attempts per email per 15 minutes, successful ones included, see
  F11). The demo server was restarted once to reset that in-memory limiter. Data persisted.

## Results: journeys x widths

| # | Journey | 390 px | 1280 px | Observation |
|---|---|---|---|---|
| J1 | First admin, login/logout, roster address, verification link | PASS | PASS | Fresh DB: admin logged in and landed on `/roster`; logout works. Golfer added, register shows the neutral "If that address is on the league roster..." message, captured link opened, verification page neutral, `/my/schedule` 200 and `/admin/*` 403 afterwards. Run once at each width with a different golfer. See F4 for the unstyled verify page. |
| J2 | CSV import: stage, correct, exclude, apply, re-import | PASS | PASS | 1280: 6 rows staged, bad handicap corrected, one row excluded, apply = 5 created 1 skipped; re-import = 0 created 5 unchanged; markup row shown as text. 390: 3 rows, correct+exclude, 2 created; re-import 2 unchanged. Apply clicked with a real click at 390. Layout defects F3 (390) and F13 (1280). |
| J3 | Course, tees with three rating scopes, hole grid | PASS | PASS | Tee 1 (390) and tee 2 (1280): front/back/full rating, slope, par read back exactly after reload. Hole grid edited via the JSON preview/save flow at both widths and read back. Course city edit read back. Invalid grid shows an error. 390: the holes page itself overflows (F1). |
| J4 | Season create, select golfers, tee/seed override | PASS | PASS | 1280: season created, 8 golfers selected and saved ("Season roster saved."), tee+seed override added (override counter 1). 390: 3 more golfers saved, second override added (counter 2). Unselected golfers and the selected ones stay "Sub" until placed on a team. The override is only visible inside a collapsed `<details>` (OBS-4). |
| J5 | Teams: complete assignment, position override | PASS | PASS | 1280: team of 4 created, "Assign positions by handicap", then P2/P3 swapped and persisted after reload. 390: second team created, P1/P4 swapped, persisted. |
| J6 | Weeks: generate typed weeks, shift preview then confirm | PASS | PASS | 10 weeks previewed then created (typed: play-with-team, rain date). 1280: shift week 3 +7 days, preview showed offsets, later weeks moved once by +7. 390: shift week 7 (real click), later weeks moved once. Replaying the confirm returned 409 (no second shift). |
| J7 | Cancelled week 1 kept, numbering unchanged | PASS | PASS | Week 1 created with status Cancelled and kept in the list; weeks 1-10 numbering unchanged after the shifts. 390 view was read-only (week list at 390 shows week 1 Cancelled). |
| J8 | Rain date preview then confirm, makeup navigation | PASS | PASS | The procedure says 9 to 10, but week 9 is "play with team" and offers no rain-date control, so a match week was used. 390 (season 2): week 2 to week 10, 1 team match and 9 snapshots moved, week 2 shows "Cancelled, Makeup: Week 10", week 10 shows "Makeup for Week 2" with the matchups and the substitute intact, and both links navigate. 1280 (demo season 1): week 3 to week 10, same result (4 team matches, 32 snapshots). |
| J9 | Matches: normal pairing (4) and self-match (2, flagged) | PASS | PASS | Normal pairings produced 4 player matches (390: wk 2, 1280: wk 5). Self-match produced 2 matches labelled P1VP2 / P3VP4 (1280: wk 4, 390: wk 6). The only "flag" is the slot label; no separate marker (OBS-6). A parallel double POST of the self-match created exactly one pairing (second got 422). |
| J10 | Substitution: blank rejected, valid sticks through regeneration | PASS | PASS | Blank substitute: 422 with an inline error ("Choose a valid player or team."), no change. Valid substitute applied, "Regenerate matches" run, substitute still shown "(sub)". 1280: Mia Mobile, 390: Noah Narrow. |
| J11 | My Schedule, four personas | PASS | PASS | See "J11 detail" below. All four personas at both widths. Contact data only via the scheduled-opponent page/dialog; non-opponent IDs 404. See OBS-5 about league-wide week pages. |
| J12 | Shared invite: create, view once, revoke, rotate, send limit, join, set-password | PASS | PASS | 1280: created, URL shown once (second GET 404), join page neutral for off-roster and roster addresses, send limit (3 mails sent, 6 suppressed out of 9 requests, always the same neutral message, counters shown in the admin list), set-password link set a password and signed in, link single-use, earlier links invalid, revoke and rotate worked (old URL 404, new URL 200). 390: create (URL fits, no overflow), join, mail, set password, sign in. Revoke/rotate were not repeated at 390. Unstyled public pages: F4. |
| J13 | `/admin/config` invalid then valid | PASS | PASS | Invalid port and sender: error summary (`role=alert`, links to fields, `aria-invalid`, `aria-describedby`). A synthetic SMTP password was saved: page shows "Currently set." and never echoes it (not in HTML), saved banner says restart required; on 1280 the password was cleared again. The managed file mode is 0600 and the secret is not in server output. |
| J14 | Back, refresh, double-submit on J2, J6, J9, J12 | FAIL | FAIL | J2 apply, J6 shift confirm and J9 pairing: a replay is rejected (409 / 422), no duplicate. Back showed the stale preview page; re-confirming it returned 409. Refreshing an applied batch re-GETs (no re-post). **J12 invite creation: two parallel POSTs created two live invites** (F5, reproduced at both widths). |

Counts: 26 PASS, 2 FAIL (J14 at both widths), 0 NOT RUN. The failure-bearing cross-checks
(overflow, target size, public-page styling) are counted under "Cross-check results" and F1-F13,
not in this table, because the journey's own "pass when" condition was met.

### J11 detail

| Persona | Observed (both widths) |
|---|---|
| `player-regular` | Weeks 2 and 3 show a matchup with a linked opponent; the opponent page and the contact dialog show email only (phone "Not recorded"). Opponent pages for any other golfer or week: 404. `/admin/*`: 403. |
| `player-sub` | Only week 2 (against Demo11) has a matchup; other match weeks read "No participation". One opponent link; other IDs 404. |
| `player-unused` | All match weeks "No participation", no opponent link; every opponent URL 404. |
| `player-unlinked` | Identical to unused. Nothing tells the user their account is not linked to a golfer (OBS-5). |

The contact dialog (`<dialog>`) opens from the opponent link with focus on the close button,
Escape closes it and focus returns to the link (1280). At 390 it is readable and fits
(close button 43x43 px). Player HTML pages contained no emails, phone numbers, `mailto:`/`tel:`
outside the opponent page.

## Cross-check results

| Check | Result | Detail |
|---|---|---|
| Authorization matrix | PASS | Anonymous: 401 on `/admin/*`, `/my/schedule`, `/weeks/*`, opponent pages; `/` and `/roster` redirect to login. Registered but unverified (mia.mobile): 403 "Email verification required" on every page. Verified non-admin: 403 on `/admin/golfers`, `/admin/seasons`, `/admin/config`, `/admin/invites`, `/admin/roster/imports/new`. Admin: 200. UX of the unverified case: F9. |
| CSRF | PASS | POST without a token returned 403 "Invalid CSRF token" for login, register, reset, logout and 19 admin endpoints (config, courses, holes, golfers, seasons, weeks, shift/rain-date confirm, generate, substitute, assign-positions, invites create/revoke/rotate, import apply/discard/row edit/purge). A bad token also 403. Two endpoints validate other fields first (import discard returned 422 for a missing `batch_version`), but with the version supplied they returned 403. |
| Tampered and cross-season IDs | PASS with F12 | 404 for unknown season/course/tee/golfer/team/import/week/player-match IDs and for admin URLs that mix a season with another season's week or player match (`/admin/seasons/1/weeks/12/edit`, `/admin/seasons/1/player-matches/33/substitute`, and so on). Non-integer IDs: 422. Exception: F12. |
| Cache-Control / Referrer-Policy on token pages | PASS for the listed pages, F10 for two others | `/join/{token}` (200 and 404), `/set-password/{token}` (404; valid-token page seen through the browser), `/admin/invites/created` (200 one-time page and the later 404) and the opponent contact pages all send `Cache-Control: no-store`; the join, set-password and created pages also send `Referrer-Policy: no-referrer`. `/verify/{token}` and `/reset/{token}` send neither (F10). |
| Long names wrap, markup escaped | PASS | The demo sub with a very long surname and a synthetic golfer with `<img src=x onerror=alert(1)>` and a 70-character surname: no `img`/`script` elements created on `/roster`, `/admin/golfers`, participants, substitute, golfer edit, import review; names wrap at 390 without page overflow (tables scroll in their own region). The reflected `?notice=` query value is also escaped. |
| Keyboard only | PASS | First Tab stop is "Skip to content" with a visible 3px outline; Enter jumps to `#main-content`; next Tab lands on the first form field (visible focus ring). Manage menu is a `<details>`: reachable by Tab, opens with Enter, first link receives focus with an outline. Form controls: no unlabelled control found on 32 pages (audited at 1280). Weak spots: F7, F8, OBS-7. |
| Touch targets >= 44 px at 390 | FAIL | See F6. Buttons, inputs, selects and the main nav meet 44 px; inline text links (about 18 px), the brand link (32 px), `<summary>` "Original CSV row" (20 px) and the unstyled public pages (21 px) do not. |
| No horizontal page scroll at 390 | FAIL | 34 pages measured by loading each in a 390-px frame and reading `scrollWidth`. 30 are exactly 390. Over: `/admin/courses/1/holes` 560, `/admin/golfers` 1168, `/admin/seasons/N/weeks` 637, `/admin/roster/imports/new` 489 (F1, F2, F3). At 1280 all 16 sampled pages were exactly 1280. |
| Wide tables scroll in their own region | PASS except F1 | Weeks, golfers, My Schedule, matchups, import review use a scrolling wrapper at 390. My Schedule hides its Matchup column off to the right at 390 (OBS-7). |
| Empty and error states | PASS with notes | Readable at both widths: empty roster, "No invites yet", "No weeks generated", "No pairings yet", invalid login, hole-grid error, config error summary, import row errors. Raw JSON error bodies for non-form errors: F9. |
| Server output | PASS | No tracebacks, no 5xx, no passwords or secrets in app output; token URLs are logged as `/join/[REDACTED]`, `/set-password/[REDACTED]`, `/verify/[REDACTED]`, `/reset/[REDACTED]`. The only credential text in the log is the harness banner printed by `run_local.py` (the documented synthetic defaults). The one traceback-like error was my own failed bind on port 8001 before using 8011. |
| Open redirect via `next=` | PASS | `https://evil.example/x` and `//evil.example/x` were ignored after login (landed on `/roster`). |

## Failures and findings (with repro)

Severity is my own judgement. Steps start from a running demo server, logged in as the admin
unless stated. Each "FAIL" is a deviation from a procedure criterion.

### F1 (FAIL, medium) Hole grid table is wider than the viewport at 390 and is not in a scroll region
- URL: `/admin/courses/1/holes`. Steps: set viewport 390 px, open the URL, scroll to the table.
- Observed: `document.documentElement.scrollWidth` = 560; the table is a direct child of
  `<main>` (no `.table-wrap`); the "Snake yards" column is cut off and cannot be reached with the
  emulated scrolling (the Deer column sits at the right edge). Expected: no page-level overflow;
  wide table inside its own scroll container.

### F2 (FAIL, medium) Visually hidden labels inside `.table-wrap` widen the page at 390
- URLs: `/admin/golfers` (`scrollWidth` 1168), `/admin/seasons/{id}/weeks` (637). Steps: open at 390 px.
- Observed: the `.table-wrap` has `overflow:auto` but `position: static`; the absolutely
  positioned `.visually-hidden` spans/labels inside it are not clipped by it and extend the page
  (their right edge is at 636 / 1167 px). A phone can then pan or shrink the page sideways.
  Expected: `scrollWidth` = 390. (A `position: relative` wrapper would contain them; not changed here.)
  Note: the built-in emulation refused programmatic horizontal scrolling, so actual panning on
  a device was not observed; the measurement is `scrollWidth`.

### F3 (FAIL, medium) Import upload page overflows at 390
- URL: `/admin/roster/imports/new`. Observed: `scrollWidth` 489; the file input is 472 px wide and
  the "CSV file format" text and the header-row sample are clipped at the right edge. Expected: fits 390.
  Also, the "Upload" button sits directly against the "Purge expired imports" heading (no spacing).

### F4 (FAIL, medium) Public token pages are unstyled
- URLs: `/join/{token}`, `/set-password/{token}` (the `/verify/{token}` result is a bare fragment).
- Observed: no stylesheet (`document.styleSheets.length` = 0), default browser styling, input and
  button about 21 px high at 390, no site chrome or "log in" link. `/verify/{token}` returns
  `<p>If that link was valid, your email address is now verified.</p>` with no `<title>`, no
  `<main>` and no way forward. Expected: the same styled layout and 44 px targets as the rest.

### F5 (FAIL, low to medium) Double submit of "Create invite" creates two live invites (J14)
- Steps: `/admin/invites/new`, fill the label, send two parallel POSTs of the form (or double-click).
- Observed: both redirect; `/admin/invites` then lists two live invites with the same label
  (reproduced twice: "Double submit" at 1280, "Mobile double" at 390). Import apply, shift
  confirm, rain-date and pairing creation are protected (409/422); this endpoint is not.

### F6 (FAIL, low) Touch targets under 44 px at 390
- Inline links measured about 18 px high: "Back to season weeks", "Add a course", "Wyandot Golf
  Club", "Seasons", "Season roster", "Teams", "Week details", "Edit", "Matches", opponent name
  links, "Back to My Schedule"; the brand link is 168x32; `<summary>` "Original CSV row" 117x20;
  contact dialog close button 43x43; public token pages 21 px (F4).

### F7 (FAIL, low to medium) `<html>` has no `lang`
- Every page rendered by the main layout (`/login`, `/roster`, admin pages) has `<html>` without
  `lang` (`document.documentElement.lang` is empty). The join and set-password pages have `lang="en"`.

### F8 (FAIL, low) No `<h1>` on several pages
- `/register`, `/reset`, `/admin/golfers/new`, `/admin/golfers/{id}/edit`, `/admin/courses`,
  `/admin/courses/new`, `/admin/courses/{id}/edit`, holes, tees, `/admin/seasons/new`,
  `/admin/seasons/{id}/edit`, `/admin/roster/imports/new` have no `<h1>` (loaded in a frame and
  counted `h1` elements).

### F9 (FAIL, low to medium) Browser-visible errors are raw JSON
- An unverified user who logs in lands on `/roster` showing `{"detail":"Email verification required"}`.
- Anonymous `/admin/*` shows `{"detail":"Not authenticated"}` (401), invalid join/set-password/reset
  tokens show `{"detail":"Not found"}`, a stale preview confirm shows
  `{"detail":"The schedule or requested change changed after this preview. Review a fresh preview."}`
  (409), a lost pairing race shows a 422 JSON body. No navigation, no explanation of next step.
  The 401/403/404 behaviour matches the matrix; the presentation is the finding.

### F10 (FAIL against the stated token-page rule, low) `/verify/{token}` and `/reset/{token}` lack token-page headers
- `curl -si http://127.0.0.1:8000/verify/x` and `.../reset/x`: no `Cache-Control: no-store` and no
  `Referrer-Policy: no-referrer` (join/set-password have both). The procedure's list names only
  join, set-password and created; these two carry tokens in the URL too.

### F11 (low) Login throttle also counts successful logins and reports "not correct"
- `app.py` `LOGIN_RATE_LIMIT = 5` per `LOGIN_RATE_WINDOW_SECONDS = 900` per email. After five
  logins in 15 minutes (including successes) the sixth returns 401 "That email or password is not
  correct." even with the right password, with no throttle message. Observed for the admin and a
  persona during this run. Neutral by design; flagged because a real user who logs in and out a
  few times is silently locked out.

### F12 (low) Week page does not 404 on a mismatched `season_id`
- `/weeks/12?season_id=1` (week 12 belongs to season 2) and `/weeks/3?season_id=2` return 200 and
  show the week's own season (links point at the correct season). No other season's data is shown,
  but the procedure expects 404 for cross-season IDs.

### F13 (low) Import review column headers drift from the inputs at 1280
- `/admin/roster/imports/{id}` at 1280: header labels sit at x = 236 / 367 / 579 / 872 / 987 / 1061
  while the inputs start at x = 275 / 436 / 697 / 1060 / 1200 / 1291, so the labels progressively
  misalign (the grid also scrolls inside its section). Functionally usable; visually wrong.

## Observations (no pass/fail criterion, for the owner)

- OBS-1: Several actions give no success message: hole grid save, "Assign positions by handicap",
  and the course/tee save (page just reloads or returns to a list).
- OBS-2: The invites table prints raw ISO timestamps with `+00:00`; the invalid-grid error reads
  "grid: Enter a complete valid grid." (field key leaks into the message); the config form resets
  an invalid entry to the saved value rather than keeping the user's input; the config default
  sender is `golfleague@aaronhatcher.com` (a personal domain, not a placeholder).
- OBS-3: No `Content-Security-Policy`, `X-Frame-Options` or `X-Content-Type-Options` headers on
  app responses from uvicorn (SWAG may add them; not checked here). Session and CSRF cookies are
  HttpOnly and SameSite=Lax; `Secure` could not be seen over plain http.
- OBS-4: Season-specific tee/seed overrides appear only in a collapsed "Season tee and handicap
  overrides" `<details>`; the participants table still shows the default tee and handicap.
- OBS-5: `/weeks/{id}` shows every league matchup and handicap to any verified player, including
  a sub with "No participation" that week and the unlinked persona (no contact data). If the
  ruling says players see only their own matches, this is wider than that; confirm against
  `Plan/Rulings.md`. The unlinked persona sees a normal-looking schedule with no hint that the
  account is not linked.
- OBS-6: A self-match is distinguished only by the slot label (P1VP2 / P3VP4).
- OBS-7: At 390 the My Schedule table is 544 px in a 358 px scroll region, so the Matchup column
  (the main content) is off screen; the scroll region has no `tabindex`/label (keyboard users can
  only scroll it by focusing a link inside). A first-week matchup for the demo is still readable
  by scrolling.
- OBS-8: A `?notice=` query value on the matches page is reflected (escaped) in the admin UI, so
  a crafted link can show arbitrary text there (no markup execution).

## What this does not prove

- SMTP delivery, TLS/STARTTLS, a real mail relay, or sender reputation. Mail was written to files.
- Anything about the Unraid/SWAG deployment: reverse proxy headers, trusted-proxy client IP,
  `https`, `Secure` cookies, persistence under `/data`, the container image or restart behaviour
  (GL-71 external evidence and GL-72 remain open). The procedure's Docker health check was not run.
- Behaviour on a physical phone or tablet, real touch gestures, iOS/Android keyboards, pinch zoom,
  or actual horizontal panning (the emulator blocked programmatic horizontal scroll, so overflow
  was shown by measured widths, not by panning).
- Other browsers (only the built-in Chromium-based pane), assistive technology (no screen reader
  run; only DOM/ARIA inspection, keyboard Tab/Enter/Escape on a few pages), contrast ratios, or
  reduced-motion behaviour. Keyboard-only was not run through every page or form.
- Human-typed input: most fields were set by script, the upload bypassed the OS file chooser,
  and real double clicks were not performed.
- Complete coverage at both widths for every state-changing step: revoke/rotate (J12) ran at 1280
  only; J7 at 390 and the week/matchup pages for several personas were read-only checks.
- Concurrency, load, session expiry, multi-admin edits, the 150-user cap, or data from the real roster.
- That the findings above are the only defects: this is a sample run on one demo season plus one
  extra season, not an exhaustive review of every page and error branch.
