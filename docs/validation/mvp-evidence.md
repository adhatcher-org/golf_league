# GL-61 browser and accessibility journey evidence

Procedure: `docs/validation/browser-journeys.md`. This file records what was actually observed in
one run in two passes. A cell is PASS only when the journey's "pass when" condition was observed;
anything not observed is NOT RUN or listed under "What this does not prove". It replaces the earlier
run recorded against `23066ad`.

**Outcome: 28 PASS, 0 FAIL, 0 NOT RUN in the journey table; 11 of 11 cross-checks PASS.**

- **Pass 1** ran every journey and cross-check against `a3738b0`. It found two failures (F1, F2).
- Both were repaired in `a3be095`. The only application files that differ between the two commits
  are `golf_league/routers/admin_invites.py`, `golf_league/services/invites.py` and
  `golf_league/routers/player_schedule.py` (invite creation, and the `/weeks/{id}` page).
- **Pass 2** re-ran, against `a3be095`, everything those files can affect: J8, J11, J12, J14 and
  cross-check 3. The rows for J1–J7, J9, J10, J13 and the other cross-checks are pass-1 observations
  carried forward, because the repair does not touch the code they exercise.

| Item | Value |
|---|---|
| Commits under test | Pass 1: `a3738b014703f9f8216e1f9d0912bb614006ebbf`. Pass 2: `a3be095cf5f08dd25c5989d1ce1d6402cf2b915f`. Both on branch `codex/m7-release-prep`, clean tree before and after each pass. |
| Date | 2026-10-11 |
| Environment | Local `scripts/run_local.py` servers (uvicorn, SQLite in a scratch directory outside the repository). Pass 1: `--demo` on port 8000 for J2–J14, and a fresh non-demo data directory on port 8011 for J1 and the empty states. Pass 2: a new `--demo` server on port 8000. Synthetic data only. Mail captured to files in `<data-dir>/mail/`. |
| Browser | The Claude desktop app's built-in browser pane (Chromium-based), viewport emulation at 390x844 and 1280x800 |
| Driven by | Claude (Opus), in the session that also wrote the GL-61 specification. Not an independent tester. |
| Not this environment | NOT SMTP delivery. NOT the Unraid/SWAG deployment. NOT HTTPS. NOT a physical phone. NOT the container (the image was only built). |
| Credentials | Only the synthetic defaults from the procedure, plus synthetic passwords made up during the run. No real `.env` was opened. Both servers were stopped and the scratch directory deleted afterwards. |
| Gate | `make check` in a clean clone: at `a3738b0` exit 0, 1012 passed, branch coverage 91.38%; at `a3be095` exit 0, 1014 passed, branch coverage 91.40%. `docker compose build`: exit 0 at both. |

## How the journeys were driven (read this before trusting a PASS)

- **Most state-changing steps were scripted, not typed.** A helper running in the page fetched each
  form, read its fields and its real CSRF token, and posted it with `fetch`. That is a real browser
  request with the real session cookie, but it is not a person filling in a form. The CSV upload was
  a `File` built in script, not the OS file chooser.
- **Real keyboard and mouse were used for:** the first-admin login (typed, 1280); logout (clicked,
  1280); a player login (typed, Enter, 390); set-password (typed and clicked, 1280); opening and
  closing the opponent contact dialog (click, Escape, 390); creating an invite, pressing Back and
  creating again (typed and clicked, 390); Tab and Enter through the skip link and the Manage menu.
- **Scripted steps do not depend on the viewport.** Each was run once, or twice on different
  records, and counts for both widths. What was checked separately at each width is rendering: every
  page of every journey was loaded at 390 px and at 1280 px and measured for sideways scroll,
  controls pushed off screen, table wrappers and control heights (39 signed-in pages, 7 public
  pages, 5 error pages, 7 empty-state pages). Those measurements were taken by loading each page in
  a same-origin frame of the stated width; `/admin/golfers`, `/my/schedule`, `/roster`, the verify
  page and the seasons empty state were also viewed directly at 390 px, and the golfer form, verify
  page and home page at 1280 px.
- **Double-submit checks were two parallel POSTs with one form's token**, not a physical double
  click. The Back check on invite creation was a real Back followed by a real click, at 390 px in
  pass 1 and at both widths in pass 2.
- **Pass 2** used the same methods: the invite form was typed and clicked at both widths, a second
  set-password was typed at 1280 px, and the rest (join, send limit, rotate, revoke, the rain-date
  move, the four personas, the ID checks) was scripted. The invite and week pages were measured again
  at both widths.

## Results: journeys x widths

| # | Journey | 390 px | 1280 px | Observation |
|---|---|---|---|---|
| J1 | First admin, login/logout, roster address, verification link | PASS | PASS | Fresh database: admin logged in and landed on `/roster`; logout returned to `/login`. Two golfers added. Registering either roster address or an unknown address returned the same page; mail was written only for the two roster addresses, with absolute `http://127.0.0.1:8011/verify/…` links, files mode 600. Before verification the account got 403 "Verify your email address"; after opening the link, `/my/schedule` 200 and `/admin/golfers` 403 "Not allowed". The verify page is styled, has one h1 and sends `no-store` / `no-referrer`. One address per width. |
| J2 | CSV import: stage, correct, exclude, apply, re-import | PASS | PASS | Six rows staged; one invalid handicap blocked apply (422, "Included rows must have no errors"). After correcting it and excluding one row: 5 created, 1 skipped. Re-import of the same file: 0 created, 5 unchanged. A first name containing an `<img onerror>` tag was shown as text on the review page and on `/admin/golfers` (no `img` element created). |
| J3 | Course, tees with three rating scopes, hole grid | PASS | PASS | City and state read back. Tee 1: front, back and full rating and slope changed and read back exactly; tee 2: three slopes changed and read back. A non-numeric slope was rejected (422). Hole grid: two yardages changed, saved with a "Saved." notice, read back; an empty grid was rejected (422). The grid table is inside a scrolling wrapper. |
| J4 | Season create, select golfers, tee/seed override | PASS | PASS | Second season created; ten golfers selected ("Season roster saved."); a tee and seed override added for one golfer (override counter 1). Selected golfers show "Sub" until placed on a team. |
| J5 | Teams: complete assignment, position override | PASS | PASS | Two teams of four created. "Assign positions by handicap" reordered the team using the season override and showed "Positions assigned by handicap."; a manual swap of positions 2 and 3 persisted. |
| J6 | Weeks: generate typed weeks, shift preview then confirm | PASS | PASS | Preview then create of ten typed weeks (play-with-team, match, rain date). Shifting week 3 by seven days: the preview listed each later week's new date; after confirm, weeks 3–10 had moved once. |
| J7 | Cancelled week 1 kept, numbering unchanged | PASS | PASS | Week 1 was created cancelled, stayed in the list, and weeks 1–10 kept their numbers through the shift and the rain-date move. |
| J8 | Rain date preview then confirm, makeup navigation | PASS | PASS | Pass 2, demo season: week 2 moved to the reserved week 10; week 2 reads "Cancelled · Makeup: Week 10" and week 10 "Makeup for Week 2", each linking to the other, and both links open (200). The four team matches and sixteen player matches moved once. Pass 1 saw the same on a second season, with a substitute carried across. See O1. |
| J9 | Matches: normal pairing (4) and self-match (2) | PASS | PASS | A normal pairing generated four player matches. A self-match generated two, in slots labelled P1vP2 and P3vP4. |
| J10 | Substitution: blank rejected, valid survives regeneration | PASS | PASS | Blank substitute: 422 "Choose a valid player or team.", no change. A valid substitute was shown "(sub)" and was still there after "Regenerate matches". |
| J11 | My Schedule, four personas | PASS | PASS | Regular: two matchups, each with an opponent link; the contact page and dialog show the opponent's email only, `no-store`. Substitute: one matchup, the rest "No participation". Unused and unlinked: no matchup and no opponent link. For all four: any other opponent URL 404, `/admin/*` 403, and no address, `mailto:` or `tel:` on the schedule or roster pages. At 390 px the schedule is a stack of cards with the matchup visible; the dialog opens with focus on a 48 px close button, Escape closes it and focus returns to the link. Pass 2: for each persona all ten "Week details" links opened (200), the makeup rows linked both ways, and a week URL with the wrong season answered 404. |
| J12 | Shared invite: create, view once, revoke, rotate, send limit, join, set-password | PASS | PASS | Observed in both passes; pass 2 values: URL shown once (a second GET of the created page is 404). Join page: one email field, autocomplete off, shows "Log in" even while an admin is signed in. Seven join requests (one off-roster, five for one roster address, one for another) returned byte-identical pages; four messages were written and the admin list showed 4 handoffs, 2 rate-suppressed. Earlier links for the same address were dead (404). Set-password: short password 422; a valid one signed the player in and redirected to `/` (scripted once, typed once at 1280 px); reuse 404. Rotate: old URL 404, new URL 200. Revoke: new URL 404. Every join and set-password response, including 404 and 422, sent `no-store` / `no-referrer`. |
| J13 | `/admin/config` invalid then valid | PASS | PASS | Invalid port and sender: 422, error summary with `role="alert"` and links to both fields, `aria-invalid` and `aria-describedby` on each, stored values re-shown. A synthetic SMTP password saved: "Currently set.", never present in the HTML, three "Restart required" markers. Cleared again: "Currently not set.". A field outside the allowlist: 422, nothing written. Managed file mode 600. |
| J14 | Back, refresh, double-submit on J2, J6, J9, J12 | PASS | PASS | Parallel double submits were all single-effect: import apply 200 and 409 (a replay 409); week creation 200 and 409; shift confirm 200 and 409 (a replay 409); self-match pairing 200 and 422 (pass 1); invite creation one invite and "submitted=again" (both passes). **Back then "Create invite" again, with real clicks at each width in pass 2: the second click landed on the invite list with "That form was already submitted, so no second invite was created", and the list held one invite with that label.** Reloading that page created nothing. In pass 1 this step created a second invite (F1, since repaired). |

Counts: 28 PASS, 0 FAIL, 0 NOT RUN.

## Cross-check results

| # | Check | Result | Observation |
|---|---|---|---|
| 1 | Authorization matrix | PASS | Anonymous: `/` and `/roster` redirect to login; `/my/schedule`, week and opponent pages and six admin URLs 401; anonymous POSTs to admin URLs 401. Unverified: 403 with a verification page. Verified non-admin: 403 on admin pages. Admin: 200. |
| 2 | CSRF | PASS | 40 POST endpoints with no token: 37 answered 403. The three import endpoints (apply, discard, row edit) answered 422 because they check the batch version first; with a version and no token each answered 403 and the batch stayed staged. Twelve endpoints with a wrong token: all 403. |
| 3 | Tampered and cross-season IDs | PASS | Unknown season, course, tee, golfer, team, import and week IDs: 404. Admin URLs mixing season 1 with season 2's week or player match: 404. A non-integer ID: 422. Pass 2: `/weeks/2?season_id=2` and `?season_id=999` answer 404, the right season and no parameter answer 200, `/my/schedule?season_id=999` 404. In pass 1 the wrong season answered 200 (F2, since repaired). |
| 4 | No contact data, tokens or passwords on player pages or in server output | PASS | See J11 and check 11. |
| 5 | Token pages send `no-store` and `no-referrer` | PASS | `/join/…`, `/set-password/…`, `/verify/…`, `/reset/…` and `/admin/invites/created`, on their 200, 404 and 422 responses. |
| 6 | Long names wrap; markup shown as text | PASS | The long demo surname and a 70-character synthetic surname wrap at 390 px without widening the page; markup in a name created no element. |
| 7 | Keyboard only | PASS | First Tab stop is "Skip to content" with a 3 px outline; Enter moves to `#main-content`; the next Tab lands on the first labelled field. The Manage menu is reached by Tab, opens with Enter, and its first link takes focus with an outline (390 px). Not run through every page. See O2 on the form-field focus style. |
| 8 | Touch targets at least 44 px at 390 px | PASS | No link, button, summary, select or input under 44 px on 58 measured pages; checkboxes sit in a 44 px label. `/admin/golfers` viewed directly: 139 controls, smallest 44 px. |
| 9 | No sideways page scroll | PASS | All 58 measured pages are exactly 390 px wide at 390 and 1280 px wide at 1280. Every table is inside a scrolling region. |
| 10 | Empty and error states readable | PASS | Empty seasons, invites and schedule on the fresh server; 401, 403, 404 and 422 pages are styled, one h1, no overflow (409 was seen only as a status code, not as a page). A client that asks for JSON still gets JSON. |
| 11 | Server output | PASS | Both servers: no traceback, no 5xx response, no password, no SMTP secret, no raw token (token paths appear as `[REDACTED]`). The only credential text is the start-up banner printed by `run_local.py` with its documented synthetic defaults. |

## Findings from pass 1, both repaired in `a3be095`

### F1 (was FAIL, medium) Back then submit created a second live invite

Pass 1, J14 on the J12 form, real clicks at 390 px: create an invite, press Back (the form comes
back with the label still filled in), click "Create invite" again. Two live invites with the same
label resulted. The single-submission value stops two posts of one form, but the invite pages are
`no-store`, so Back fetched the form again with a fresh value.

Repair: creating an invite is refused when the same admin already has a live invite with the same
label created in the last ten minutes; the admin is sent to the invite list with the existing
"already submitted" notice. A different label, the same label after a revoke, and rotation are not
affected. Tests: `tests/test_invites.py::test_back_then_resubmit_with_a_fresh_form_makes_no_second_invite`
and `::test_same_label_is_allowed_again_after_a_revoke_or_after_the_window`. Re-observed in pass 2
at both widths.

### F2 (was FAIL, low) The week page ignored a wrong `season_id`

Pass 1, cross-check 3: `/weeks/12?season_id=1`, for a week belonging to season 2, answered 200. It
showed nothing the correct URL did not.

Repair: `/weeks/{id}` answers 404 when `season_id` is given and is not the week's own season.
`tests/test_week_view.py::test_week_detail_admin_link_and_self_match_explanation` previously
requested the page with `season_id=999` and expected 200; it now expects 404 for a wrong season and
200 for the right one. Re-observed in pass 2.

## Observations (no pass/fail criterion, for the owner)

- O1: A repeated confirm of a rain-date move answers 200, where every other confirm answers 409 to
  the second. It is a harmless repeat: in pass 2 a replay after the move left the makeup week with
  the same four team matches and sixteen player matches.
- O2: A focused text field, select or textarea has `outline: none`. What marks it is a 1 px border
  change to a mid green and a 3 px ring at 18% opacity. It is visible in a screenshot, but the
  border is roughly 2.8:1 against the page and 3.1:1 against the field, which is weak. Buttons,
  links and the menu use a 3 px gold outline.
- O3: `/weeks/{id}` shows every league matchup and handicap for that week to any verified player,
  including a player with no part in it (no contact data). Unchanged from the previous run.
- O4: The invites table prints raw ISO timestamps with `+00:00`.
- O5: Findings from the previous run that were looked for and not seen this time: the hole-grid,
  golfer-list, week-list and import-upload pages overflowing at 390 px; unstyled public token pages;
  small touch targets; missing `lang`; pages without an h1; raw JSON error bodies in the browser;
  missing headers on verify and reset pages; the My Schedule matchup column off screen.

## Contracts and the tests that assert them

The map follows the header of `tests/test_contract_regressions.py`. Every test named here appears as
PASSED in the clean-clone gate log at `a3be095`; the two that pass 1 could not place by name
(contracts 4 and 5) were read and confirmed for this table.

| # | Contract | Test |
|---|---|---|
| 1 | No golfer default-role column, participant role column or tee-handicap table | `tests/test_contract_regressions.py::test_migrated_schema_has_no_role_columns_or_tee_handicap_table` |
| 2 | Golfer email nullable; a CSV row without an email is an error | `tests/test_roster.py::test_two_golfers_may_both_have_no_email`; `tests/test_admin_imports.py::test_sub_missing_email_csv_reports_email_missing_and_email_malformed` |
| 3 | Negative and zero handicaps import cleanly; blank means missing | `tests/test_contract_regressions.py::test_negative_and_zero_handicaps_import_without_errors_or_warnings`; `tests/test_roster.py::test_blank_handicap_is_null_and_zero_is_scratch` |
| 4 | An import keeps one handicap per row, from the selected tee's column (regular) or the single column (substitute); a season seed override outranks the default | `tests/test_roster_import_parse.py::test_selected_handicap_follows_the_tee_label_for_regulars_and_the_single_column_for_subs`; `tests/test_week_handicaps.py::test_current_effective_seed_first_creation_order_and_historical_reuse`; `tests/test_participants.py::test_optional_override_falls_back_and_seed_override_accepts_signed_and_zero` |
| 5 | The role helper derives a role from membership alone, in a module with no ORM or HTTP import | `tests/test_participants.py::test_role_and_sub_pool_are_derived_from_membership_facts`; `tests/test_architecture.py::test_domain_has_no_forbidden_imports` |
| 6 | Substitute from an optional roster row | `tests/test_substitutions.py::test_selected_player_without_team_can_sub_and_core_does_not_commit` |
| 7 | Seeded course totals | `tests/test_course_seed.py::test_seeded_tee_totals_are_5707_and_4967`; `tests/test_holes.py::test_eighteen_holes_and_thirty_six_yardages_round_trip` |
| 8 | Eight teams, seven match weeks, 28 unique pairings | `tests/test_contract_regressions.py::test_eight_teams_seven_match_weeks_form_exactly_28_unique_pairings` |

## Accepted limitations

None. Nothing is left failing or not run, so nothing needs accepting.

## What this does not prove

- SMTP delivery, TLS, a real relay or sender reputation. Mail was written to files.
- Anything about the Unraid/SWAG deployment: the proxy, trusted client address, HTTPS, the `Secure`
  cookie flag (the local origin is plain HTTP), persistence, the image at run time, restarts.
- Behaviour on a physical phone or tablet, real touch, on-screen keyboards, pinch zoom, or real
  sideways panning. Overflow was shown by measured widths.
- Other browsers, a screen reader, contrast ratios beyond the one estimate in O2, or reduced motion.
  Keyboard-only use was sampled on three pages, not run through every form.
- Human-typed input for most forms, and a physical double click.
- Revoke and rotate, the hole grid and the config form were exercised once each, by script; their
  pages were measured at both widths but the actions were not repeated per width.
- Concurrency beyond two parallel requests, load, session expiry, two admins editing at once, the
  150-account cap, or the real roster.
- That F1 and F2 were the only defects. This was one demo season plus one extra season, not an
  exhaustive review of every page and error branch.
- A full repeat at `a3be095`. Ten journeys and ten cross-checks rest on pass-1 observations at
  `a3738b0`, on the reasoning that the three changed files cannot affect them.
