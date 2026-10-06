# Golf League Application

Golf League is a FastAPI application for managing a league roster, seasons, teams, schedules, and
published matchups.

## Local development

Run the app with `docker compose up --build`. Provide `SESSION_SECRET` and, for first boot,
`ADMIN_EMAIL` and `ADMIN_PASSWORD` through the shell or an untracked `.env` file.

`EMAIL_VERIFICATION_REQUIRED` defaults to `true`. Set it to `false` only for trusted local or
development deployments. Existing unverified accounts can then use player pages, and newly
registered accounts are marked verified without sending a verification email. Production should
keep the default.
