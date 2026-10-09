# Release checklist (MVP)

Fill in privately for the exact commit being released; an unchecked line is not done. Results
belong in the release record (vault task records), not in this file.

## Code and build

- [ ] Commit SHA recorded; `git status` clean.
- [ ] `make check` exit 0 on that commit: tests passed count, branch coverage % (>= 80).
- [ ] Pre-PR review recorded through `pr-gate` for that SHA; PR merged to `main`.
- [ ] CI green on `main`; publish workflow pushed the image; tag (`YYYY.MM.DD-N`) and digest
      recorded.
- [ ] `docker compose -f deploy/compose.example.yml --env-file <placeholder env> config --quiet`
      exit 0.

## Evidence (GL-61)

- [ ] Browser journeys J1–J14 at 390 px and 1280 px (`docs/validation/browser-journeys.md`):
      pass / fail / not run per journey, with screenshots location.
- [ ] Cross-checks: authorization matrix, CSRF, ID tampering, privacy of player pages and logs,
      no-store/no-referrer on token pages, keyboard and 44 px targets, no sideways scroll.
- [ ] Server logs of the run: no tracebacks, no secrets.
- [ ] Unresolved decisions and untested external prerequisites listed.

## Operations (GL-71)

- [ ] Disposable-container drill: managed setting survives restart; backup restored into an
      isolated directory; synthetic login verified; client IP via trusted proxy only.
- [ ] Relay authentication and SPF/DKIM/DMARC verified by the operator (result only).
- [ ] DNS and SWAG certificate for the public host verified.

## Deployment (GL-72, needs authorization)

- [ ] Owner authorization recorded for the window.
- [ ] `docs/deployment-runbook.md` steps A–G followed; outcomes recorded without secrets.

## Known limitations to state in the release record

- Single worker by design (process-local invite receipts and rate limiters).
- Every managed setting needs a container restart.
- No scoring, results, standings, statistics, automatic pairing or handicap recomputation.
- Mail has no retries or outbox; a failed message is retried by the user asking again.
