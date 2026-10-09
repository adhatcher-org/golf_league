# Configuration

Governing decision: `R-DEPLOYMENT` (v2) in the project's Rulings. This page describes what
the code does; the ruling is the authority for what it must do.

## Two kinds of setting

**Deploy-time.** Never editable or displayed by the application: `SESSION_SECRET`,
`DATABASE_URL`, `EXTERNAL_BASE_URL`, `MAX_USERS`, `EMAIL_VERIFICATION_REQUIRED`, `ADMIN_EMAIL`
and `ADMIN_PASSWORD` (`DEPLOY_FILE_KEYS`). Each is read from the container environment and,
when the environment does not set it, from the managed file (below). `MANAGED_ENV_PATH` is
read from the environment only. `SESSION_SECRET` has no default: if neither the environment
nor the file sets it, startup fails with "SESSION_SECRET is not set" and the file path checked.

**Managed.** Exactly eight keys an administrator may change at `/admin/config`
(`golf_league/managed_config.py`, `MANAGED_KEYS`):

| Key | Type | Allowed values | Default |
|---|---|---|---|
| `SMTP_HOST` | text, optional | host name or IP address | empty |
| `SMTP_PORT` | whole number | 1 to 65535 | `587` |
| `SMTP_USERNAME` | text, optional | single line, at most 254 characters | empty |
| `SMTP_PASSWORD` | secret, optional | single line, at most 1024 characters | empty |
| `SMTP_FROM_EMAIL` | email | an email address | `golfleague@aaronhatcher.com` (proposed) |
| `SMTP_FROM_NAME` | text | single line, at most 120 characters | `St. Paul Golf League` (proposed) |
| `SMTP_TLS_MODE` | choice | `starttls`, `ssl`, `none` | `starttls` |
| `LOG_LEVEL` | choice | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |

`LOG_LEVEL` sets the level of the application's own `golf_league` loggers, which write
redacted lines to stdout. Uvicorn's own loggers are not affected.

## Mail transport

Mail is sent through SMTP (`SmtpEmailSender` in `golf_league/services/email.py`, standard
library only) when `SMTP_HOST` is set, and captured in memory (`FakeEmailSender`) otherwise.
The choice is made once, at startup, so changing it — like any SMTP setting — needs a restart.
Each message opens one connection with a 15-second timeout. The three `SMTP_TLS_MODE` values:

- `starttls` (default, port 587): connect in plain text, then upgrade with STARTTLS using a
  certificate-verifying default TLS context, before logging in.
- `ssl` (usually port 465): TLS from the first byte, also with a certificate-verifying context.
- `none`: no encryption at all; only for a relay on a trusted local network.

The sender logs in only when `SMTP_USERNAME` is set. Every emailed link (password reset, email
verification, shared-invite set-password) is built from `EXTERNAL_BASE_URL`, never from the
request's `Host` or forwarded headers. Mail is sent after the HTTP response has been produced,
in a background task: a slow or failing relay cannot delay or change the response, and a
failure is logged as one fixed line without the address, link or error text. There are no
retries and no outbox; a user whose mail failed simply asks again.

## The managed file

- One file, located by the environment-only `MANAGED_ENV_PATH`; default `./data/.env`,
  which is `/app/data/.env` in the container — inside the mounted data directory.
- Mount the **directory**, never the single file: the application replaces the file
  atomically (temporary file in the same directory, `fsync`, rename), and a rename cannot
  cross a single-file bind mount.
- Precedence, highest first: arguments passed to `Settings(...)` directly (tests only); the
  eight **managed** keys from the file; the container environment; the **deploy-time** keys
  from the file; the code default. So the file overrides the environment for the eight managed
  keys, while the environment overrides the file for the deploy-time keys.
- On Unraid the deploy-time values (including `SESSION_SECRET` and the first-admin values) may
  therefore live in this file in the data directory instead of the container template. Keep it
  mode `0600` and owned by the container user; it is part of the private backup.
- `ADMIN_EMAIL` and `ADMIN_PASSWORD` reach the first-boot admin bootstrap the same way:
  environment first, then the file.
- An empty value (`ADMIN_PASSWORD=`), in the file or in the environment, counts as unset. Any key outside the eight managed keys
  and the seven deploy-time keys has no effect; `MANAGED_ENV_PATH` in the file has no effect.
- A managed value in the file that fails validation (for example a hand-edited
  `SMTP_PORT=abc`) is ignored and logged by key name only; the environment or default value
  applies. An unreadable file is ignored the same way. Neither stops the application starting.
- Format: one `KEY=value` per line. The application writes `KEY="value"` with `\` and `"`
  escaped. It reads double-quoted, single-quoted and bare values; there are no inline
  comments, no variable expansion and no shell evaluation.
- Writes keep every line the application does not own (comments, deploy-time keys, other
  keys) byte for byte, replace a
  managed key in place, drop later duplicates of that key, and append new keys. The file is
  written with mode `0600`. A failed write removes its temporary file and leaves the previous
  file unchanged; the form reports "Nothing was changed" with status 503.
- Saving writes only the keys whose value changed.

## Taking effect

Every managed setting is **restart-required**. Settings are resolved once, when the process
starts, and kept on `app.state.settings`. After a save, `/admin/config` marks each setting
whose saved value differs from the running value as "Restart required". The application never
restarts itself; restarting the container is an operator action.

## Secrets

`SMTP_PASSWORD` is never rendered back into the form, never logged and is excluded from
`repr(Settings)`. The form shows only whether a password is set. A blank password field keeps
the stored value; only the explicit "Clear" checkbox empties it (an empty value in the file
then overrides the environment). Submitting a new value and "Clear" together is rejected.

## The form

`GET /admin/config` and `POST /admin/config` require a verified admin (anonymous 401,
non-admin 403) and a CSRF token bound to the session cookie (missing or wrong: 403). A POST
containing any field outside the eight keys, their clear control and the CSRF token — or the
same field twice — is rejected with 422 and writes nothing. Invalid values re-render the
stored values with an error summary and 422. Success redirects 303 to `/admin/config?saved=1`.
Responses are `Cache-Control: no-store`.
