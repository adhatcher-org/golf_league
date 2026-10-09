# Deployment: Unraid behind SWAG

Governing decision: `R-DEPLOYMENT` (v2). Templates: `deploy/compose.example.yml` and
`deploy/swag.example.conf`. Every value shown as `<...>` or `${...}` is a placeholder; the
operator records real values privately on the host, never in this repository or the vault.

This page prepares a deployment. It does not authorize one: changing the live Unraid host,
DNS, SWAG or sending real mail needs the owner's explicit authorization (GL-72).

## Shape

- One application container (`golf-league`), one uvicorn worker. The invite receipt display
  and the managed-settings write lock are process-local, so do not add workers or replicas.
- The existing SWAG container terminates HTTPS and proxies to the app over plain HTTP on the
  shared Docker network. No Apache, no second proxy, no load balancer. The app publishes no
  host port.
- SQLite and the managed settings file live in one host directory mounted at `/app/data`.

## Values the operator supplies privately

| Placeholder | Meaning |
|---|---|
| `GOLF_LEAGUE_IMAGE` | `ghcr.io/adhatcher-org/golf_league@sha256:<digest>` (or a release tag). Record the digest you deploy; never use a moving `latest`. |
| `GOLF_LEAGUE_DATA_DIR` | Host directory, e.g. `/mnt/user/appdata/golf-league`. Mounted as a directory. |
| `PUID` / `PGID` | Owner of that directory. The image user is `1000:1000`; either `chown 1000:1000` the directory or set these to its owner. |
| `SESSION_SECRET` | Long random value. Rotating it signs everyone out and resets the invite-quota keys. |
| `EXTERNAL_BASE_URL` | `https://golfleague.aaronhatcher.com`. Every emailed link and invite URL is built from it. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | First boot only; ignored once any user exists. Remove after the first admin logs in. |
| `PROXY_NETWORK` | The user-defined Docker network SWAG is attached to. |
| `SWAG_IP` | SWAG's fixed address on that network (see "Client IP" below). |
| SMTP relay values | Entered at `/admin/config` after first login (stored in `/app/data/.env`, mode 0600), or as `SMTP_*` environment variables. |

## Client IP and HTTPS behind the proxy

The app runs uvicorn with its default proxy-header handling, which trusts
`X-Forwarded-For` and `X-Forwarded-Proto` **only** from the addresses in
`FORWARDED_ALLOW_IPS`. The compose template sets that to `SWAG_IP` and nothing else.

- SWAG's stock `proxy.conf` appends the real client to `X-Forwarded-For`. Uvicorn walks that
  list from the right and uses the first address that is not a trusted proxy, so a client that
  sends its own `X-Forwarded-For` cannot choose the address the per-IP invite quota sees.
- A request that reaches the app from any other address (another container, a LAN host) has its
  forwarded headers ignored; its own socket address is used.
- Give SWAG a fixed address on the proxy network (`docker network connect --ip <SWAG_IP>
  <PROXY_NETWORK> swag`, or the network's static-IP field in the Unraid template). Trusting the
  whole subnet instead would let any container on that network forge client addresses.
- **Never** set `FORWARDED_ALLOW_IPS=*`.
- Emailed links, invite URLs and the secure flag on the invite receipt cookie come from
  `EXTERNAL_BASE_URL`, never from `Host`, `X-Forwarded-Host` or `X-Forwarded-Proto`.
- The session and CSRF cookies are not yet marked `Secure` by the application. The SWAG
  template adds the flag at the TLS edge with `proxy_cookie_flags ~ secure;`. Keep that line.

## Startup, migrations and health

On every start the container runs, in order: resolve settings (environment plus the managed
file) → apply `LOG_LEVEL` → choose the mail transport (SMTP when `SMTP_HOST` is set, otherwise
in-memory) → create the engine → `alembic upgrade head` → first-admin bootstrap (only when
`users` is empty) → idempotent Wyandot course seed. A database or migration failure does not
crash the process: it stays up and `/readyz` answers 503.

- `/healthz` — process only; always 200 while uvicorn runs.
- `/readyz` — 200 only when startup completed and the database answers. The image and compose
  healthchecks probe `/readyz`, so a failed migration shows as *unhealthy*.

Migrations are forward-only. There is no downgrade: rolling back an image after a migration
means restoring the backup taken before the upgrade (see `docs/backup-restore.md`).

## Managed settings

See `docs/configuration.md`. In short: eight keys (`SMTP_*`, `LOG_LEVEL`) are editable at
`/admin/config`; they are stored in `/app/data/.env` (`MANAGED_ENV_PATH`), which outranks the
container environment; every change takes effect only after the operator restarts the
container. Because the file lives in the mounted directory it survives image replacement.

## Mail

- Relay on port 587 with `SMTP_TLS_MODE=starttls` and authenticated login, unless the relay
  requires otherwise.
- Before the first real invite send, the operator verifies relay authentication and that SPF,
  DKIM and DMARC are correct for the sending domain, and records the result privately without
  credentials or message bodies. Many roster addresses are on large consumer mail providers that
  treat burst sends from a new sender as spam.
- Automated tests never send mail; a passing local run is not evidence of delivery.

## Logs and retention

- The app logs to stdout/stderr only. Application lines pass through the redaction filter
  (emails, token-like values, token path segments, query strings); uvicorn access lines have
  token path segments redacted. Invite counters on `/admin/invites` show volume and
  suppression without addresses or tokens.
- Docker keeps at most `max-size × max-file` (50 MB) of logs per container with the template's
  `json-file` options. Do not ship these logs elsewhere without the same redaction.
- Staged roster imports expire 90 days after staging and are deleted by the admin-only purge
  action on the import pages; nothing is purged automatically.

## Rollout checklist (for the authorized deployment)

1. Record the current running image digest, compose file and mounts.
2. Take and verify a backup (`docs/backup-restore.md`).
3. Validate the host copy: `docker compose --env-file <private env> -f <copy> config --quiet`.
4. `docker compose pull && docker compose up -d`; watch `docker logs golf-league` for
   migration and startup lines and confirm the container becomes healthy.
5. Through SWAG: `https://<domain>/readyz` is 200; log in as admin; open `/admin/config`.
6. Check one request's client address in the access log is the real client, not SWAG.
7. Rollback: stop the container, restore the pre-upgrade backup, start the previous digest.
