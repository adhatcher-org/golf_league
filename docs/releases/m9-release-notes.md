# M9 release notes: player score entry and week scores (draft)

Tag: assigned by the publish workflow when the release commit lands on `main`; record the tag
and image digest here when known. Governing ruling: R-SCORES version 2.

M9 shipped in three parts: release A (GL-83, GL-84; pull request #34), and GL-85, GL-86 and
GL-87 together after it.

## For players

- **Enter scores.** From a week's play date onward, My Schedule, the week page and home show
  "Enter scores" for a week you play. Type the nine scores for you and your opponent and save.
  There is no end date; the admin's lock is the only cutoff.
- **Change scores.** You, your opponent, and whoever first entered a golfer's scores can change
  them ("Update scores") until the league admin locks the match; then the link reads "View
  scores" and the match is read-only. A save made from a page that is out of date is refused
  and the saved scores are shown, so nobody overwrites a change they did not see.
- **Scorecards.** Instead of typing, upload a photo of the card or type a whole card. A card
  holds your own match and at most one other match a week, and only
  adds scores: golfers already saved, or in a locked match, are left off and named. You check
  every name and score before anything is saved. Photo uploads are limited to six an hour per
  player and are refused while eight cards are waiting to be read; typing is never limited.
  Only you can open your card; its photo is deleted on submit, on discard and after 24 hours.
- **Week scores.** Every verified player can see each week's scores ("Scores" on the week
  page), with each match's state in words and no name of who entered or changed a score.

## For admins

- **Week sheet** at `/admin/weeks/{week}/scores` (a "Scores" link on each match week): every
  match of the week; type the missing scores of several matches and save them at once.
- **Match page:** change saved scores, lock, unlock, or clear (with a confirmation).
- **Lock all** locks every fully entered match that has not changed since the page was shown,
  and names the ones it left open. A locked match refuses every write, an admin's included.
- **Enter scorecard** works as before for any week, at any time; a card started by a player
  is marked "started by a player" in the list of cards in progress, and an admin can still
  open, submit or discard it there.
- A week with a saved score or a locked match cannot be moved to a rain date; clear the scores
  and unlock the matches first.

## Upgrade

- **Back up first.** Take a verified database backup (`docs/backup-restore.md`, Option A)
  before the upgrade. There is no downgrade in production.
- **One migration**, `e41c7f9a2b36` (release A), creates `match_score_states` and gives every
  match that already has scores a revision-1 row taken from its latest score. It runs at
  startup. GL-85 to GL-87 add no migration.
- **Proxy upload limit.** Player photo uploads need the reverse proxy to accept an 11 MB body:
  `client_max_body_size 11m` in `deploy/swag.example.conf`. Confirm it on the live proxy before
  players use photos; until then they can type their cards.
- Nothing new is stored on disk; scorecard photos stay in `<data dir>/scorecards/` and are not
  backed up.

## Not checked by the implementer

The live proxy's upload limit, photo reading with the real reader from several phones at once,
a real week entered from paper, and the migration on a copy of production data. These are the
owner's manual steps for GL-86 and GL-87.
