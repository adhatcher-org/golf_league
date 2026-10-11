# Deployment runbook (GL-72)

**Status: ready for authorization — not executed.** Nothing in this repository performs these
steps. Each step that touches the Unraid host, SWAG, DNS, the mail relay or real people needs
the owner's explicit go-ahead for that session. Real values (`<...>`) are kept privately by the
operator and never written into this repository, the vault or a ticket.

Background: `docs/deployment.md` (shape and trusted proxy), `docs/configuration.md`
(settings and precedence), `docs/backup-restore.md`, templates in `deploy/`.

## A. Before the window (no production change)

1. **Pick the release.** The commit is merged to `main`, CI is green (`make check`), and the
   publish workflow has pushed the image. Record the tag (`YYYY.MM.DD-N`) and the image
   digest: `docker buildx imagetools inspect ghcr.io/adhatcher-org/golf_league:<tag>`.
2. **Release evidence.** GL-61 journeys recorded at 390 and 1280 px against this commit (or
   explicitly "not run" with the owner's acceptance); `docs/release-checklist.md` filled in.
3. **Prepare host values privately** (`docs/deployment.md`, "Values the operator supplies"):
   image digest, data directory, PUID/PGID, `SESSION_SECRET`, first-admin email/password,
   proxy network name, SWAG's fixed IP. Decide where `SESSION_SECRET` lives: the private
   compose env file, or `<data dir>/.env` (mode 0600).
4. **Mail prerequisite.** Confirm relay credentials work and SPF, DKIM and DMARC pass for the
   sending domain (a mail-tester style check from the relay). Record pass/fail only.
5. **DNS/TLS.** `golfleague.aaronhatcher.com` resolves to the SWAG host and SWAG's certificate
   covers it (SWAG `EXTRA_DOMAINS`/`SUBDOMAINS` or wildcard). Record the check, not secrets.

## B. Record the current state (read-only on the host)

6. `docker ps --filter name=golf-league --format '{{.Image}} {{.Status}}'` and
   `docker inspect golf-league --format '{{.Image}}'` (skip on first install).
7. Save copies of the current compose file, the SWAG proxy-conf and the data directory
   listing (`ls -la <data dir>`), with times. Note mounts, networks and ports.

## C. Back up (skip only on a first install with no data)

8. Online backup with the SQLite backup API and a private copy of `<data dir>/.env`
   (`docs/backup-restore.md`, option A). Check `integrity_check` prints `ok`. Copy the backup
   off the app directory.

## D. Deploy

9. Copy `deploy/compose.example.yml` to the host location, fill placeholders from step 3,
   then validate: `docker compose --env-file <private env> -f <compose> config --quiet`.
10. Ensure SWAG has its fixed IP on `<proxy network>` (`docker network inspect <proxy
    network>`), and the data directory exists, owned by `PUID:PGID`, mode 0700.
11. Install `deploy/swag.example.conf` as
    `/config/nginx/proxy-confs/golfleague.subdomain.conf` in SWAG; `docker exec swag nginx -t`;
    `docker restart swag`.
12. `docker compose --env-file <private env> -f <compose> pull` then `... up -d`.
13. Watch startup: `docker logs -f golf-league` until "Application startup complete";
    `docker inspect -f '{{.State.Health.Status}}' golf-league` becomes `healthy`. A failed
    migration shows `/readyz` 503 and an unhealthy container — go to F.

## E. Verify (through SWAG, over HTTPS)

14. `curl -sS https://golfleague.aaronhatcher.com/readyz` → `{"status":"ok"}`;
    `curl -s -D - -o /dev/null https://golfleague.aaronhatcher.com/login` → 200 and a
    `set-cookie: csrf_seed=…` header that includes `HttpOnly`, `SameSite=lax` and `Secure`.
15. Log in as the first admin; open *Manage → Configuration*; enter the relay values; save;
    `docker restart golf-league`; confirm no "Restart required" markers remain.
16. Client IP: request a page from a phone on mobile data; the access log line in
    `docker logs golf-league` shows that public address, not SWAG's.
17. Persistent data: `docker restart golf-league`; data and login survive.
18. Remove `ADMIN_EMAIL`/`ADMIN_PASSWORD` from the environment / data-dir `.env` after the
    admin exists (they are ignored once any user exists).
19. Controlled mail test (authorized, one message): `/reset` for the operator's own roster
    address; the message arrives, its link opens `https://golfleague.aaronhatcher.com/reset/…`.
20. Shared link (authorized, controlled): create an invite, join with the operator's roster
    address (one message arrives) and with a non-roster address (no message; identical neutral
    page). Revoke or rotate the test invite afterwards. Never mail real members for testing.
21. Player view: a verified player persona (the operator's own linked account) opens My Schedule.
22. Logs: `docker logs golf-league 2>&1 | grep -c Traceback` is 0; no addresses, tokens or
    passwords appear.

## F. Roll back (if any of D–E fails and cannot be fixed forward)

23. `docker compose ... stop golf-league`.
24. Restore the step-8 backup (database and private `.env`) into the data directory.
25. Set the image back to the digest recorded in step 6 and `up -d`; repeat step 14.
26. Never start an older image against a database a newer image has migrated.

## G. Record and hand off

27. Record privately: date/time, image tag and digest, health results, steps 14–22 outcomes,
    backup location and restore check, rollback (if any), open limitations. No secrets, real
    addresses or message bodies.
28. Mark GL-72 done in the vault only with that evidence; otherwise leave it blocked with what
    remains.
