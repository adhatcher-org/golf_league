# 4. Managed configuration and deployment behind SWAG

## Status

Accepted (R-DEPLOYMENT v3), implemented in M7 by GL-44, GL-45, the deploy-key fallback fix and
the Secure-cookie change (2026-10-08).

## Context

The app runs as one container on Unraid behind SWAG, which terminates TLS. The administrator
needs to set mail-relay and log settings without editing the container template, and the
Unraid setup keeps deploy-time secrets in the data directory rather than in the template.

## Decision

- **One settings file** in the mounted data directory (`MANAGED_ENV_PATH`, default
  `./data/.env`), mode 0600, replaced atomically; the directory is mounted, not the file.
- **Two key sets in that file.** Eight managed keys (`SMTP_*`, `LOG_LEVEL`) editable at
  `/admin/config`, ranked above the environment. Seven deploy-time keys (`SESSION_SECRET`,
  `DATABASE_URL`, `EXTERNAL_BASE_URL`, `MAX_USERS`, `EMAIL_VERIFICATION_REQUIRED`,
  `ADMIN_EMAIL`, `ADMIN_PASSWORD`) ranked below the environment and never displayed or
  rewritten. Empty values are unset. All settings are restart-required.
- **Mail** through `SmtpEmailSender` (stdlib, verifying TLS) when `SMTP_HOST` is set, chosen at
  startup, sent after the response; otherwise captured in memory.
- **Origin-derived security.** Emailed links, invite URLs and the cookie `Secure` flag come
  from `EXTERNAL_BASE_URL`, never from `Host` or forwarded headers. Client IP comes from
  uvicorn's proxy handling with `FORWARDED_ALLOW_IPS` limited to SWAG's address.

## Consequences

- The data-directory `.env` is secret-bearing and must be in the private backup.
- An operator can lock a value by setting it in the environment only for deploy-time keys;
  a managed key saved in the file overrides the environment until changed in the form.
- Local development needs an `http://` origin to get non-Secure cookies (`scripts/run_local.py`
  does this); tests use an https base URL.
- Changing mail or log settings needs a container restart, which the app never performs.
