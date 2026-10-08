# ADR 0003: Shared invitations authorize roster-bound credentials

Status: Accepted for GL-42 (R-SHARED-LINK v2). GL-43 anonymous HTTP,
transport and credential completion are implemented; focused validation passed
on 2026-10-07. The committed-candidate fresh-check remains the release gate.

A shared invitation is a reusable, digest-only parent authorization, not an
account or reset token. Its raw URL uses EXTERNAL_BASE_URL, never inbound Host or
forwarded host. Parents default to 30 days, expire at the exact boundary, and
can be revoked. Rotation revokes the parent and outstanding children atomically,
creates a fresh 30-day parent, and preserves historical counters on the old row.

Personal set-password credentials live in a separate table with required golfer
and invitation foreign keys, normalized email snapshot and nullable expected
existing user. UserToken retains its required user subject and existing purposes.
Active roster identity and account linkage must still match at preview and
completion; a no-user credential becomes stale if registration creates a user.
Issuance revokes previous outstanding golfer credentials across invitations.
Credentials expire after 45 minutes. No accounts or golfers are created at issue.
Phone-last-four defaults false without duplicated phone data or an MVP UI; true
fails closed until an authorized challenge implementation exists.

GL-43 completion must acquire BEGIN IMMEDIATE before reading, recheck parent,
credential and identity, conditionally consume and create/update the account in
one transaction. New users are verified, non-admin, roster-linked and use roster
display names. Existing users retain identity/role/display fields, replace their
password and increment session_version. Revoke all outstanding golfer credentials
and the user's reset/verification tokens together. Cooperating ordinary reset
completion must use the same write serialization and validity recheck. No helper
may commit half the operation. The fixed 150-account cap is checked both at
issuance and creation; existing-account reset remains available at capacity.

Send reservations use persistent SQLite fixed windows: email 3/900 seconds, IP
10/3600, invite 40/3600 and global 100/3600. Check all four before eligibility;
rate refusal increments suppressed_count once and debits none. Unknown, malformed,
inactive, identity-conflicting or capacity-refused requests debit none. Only an
eligible admitted sender invocation reserves all four and issues a token under
one BEGIN IMMEDIATE transaction. Purpose-separated HMAC keys conceal email/IP
values; expired windows are pruned opportunistically. Independent connections
and restarts share quotas. Exact window boundaries start new windows. Rotation
preserves email/IP/global budgets; failures never refund or automatically retry.
Secret rotation changes quota keys and invalidates sessions: it is an explicit
deployment event. Shared-link limits do not change login/registration/reset policy.

After the admission commit, GL-43 returns the neutral response before dispatch
through a response background callback with a fresh DB session. Successful sender
handoff increments send_count and last_sent_at (not confirmed receipt/delivery).
A sender exception revokes that issued token, increments failed_send_count, logs
only a generic failure and preserves the neutral response. Suppression is separate.
Crashes may lose mail after reservation and need not fabricate an outcome. There
is no durable outbox or delivery guarantee; a retry is a new quota-bound request.
Nuisance mail remains possible to known roster addresses within the ceilings.

All CSRF-valid public branches use the same neutral body, headers and cookies,
with a 100ms foreground floor using injectable monotonic clock/async sleeper.
GL-43 measures ASGI response-frame completion with delayed fake transport;
TestClient duration includes background work and is insufficient evidence.
Database work over the floor can still reveal branch-correlated timing. Release
verification must flag reproducible overruns; exact identical timing is not
promised. No raw tokens, recipients, limiter keys or complete credential URLs
may enter logs; token-bearing access paths must be redacted by HTTP integration.

Admin POSTs preserve 303 through an opaque nonce cookie bound to admin ID and
session-cookie digest. A locked, five-minute process-memory receipt displays
its raw URL once, with no-store/no-referrer; replay, foreign or expired receipts
fail. Raw tokens are absent from cookies, redirects, persistent storage and lists.
Single-worker deployment is required for receipt availability, not quota safety.
Restart or lost display requires rotation; irreversible digests cannot recover it.

Public join/set-password and one-time display use no-store/no-referrer. IP quota
identity comes only from request.client after explicitly trusted SWAG proxy
processing, never arbitrary forwarded-header parsing. GL-43/GL-71 own proxy
verification. Before real mail deployment, SPF, DKIM and DMARC must be correct;
real transport and deployment retain their authorization gates. GL-42 uses only
synthetic fixtures and sends no real mail.

## GL-43 implementation and verification (2026-10-07)

The public `/join/{token}` and `/set-password/{token}` routes use standalone
anonymous templates, `csrf_seed`, no-store/no-referrer headers and the configured
external origin. Join admission starts with `BEGIN IMMEDIATE` and commits its
reservation before the response callback invokes the fake email sender. The
callback records success or failure using a fresh session. Sender errors and
outcome-recording errors are logged as separate generic messages; a recording
failure after a successful send does not retry the sender or claim delivery
failure. The response floor is measured against injected monotonic time and uses
an async sleeper.

Shared completion and ordinary reset each conditionally consume their own
credential and update the password, session version and sibling revocations in
one caller-owned serialized transaction. Ordinary reset remains logged out;
shared completion establishes the new signed session only after commit. Both
paths revoke outstanding password-changing credentials and applicable
verification credentials without clearing the consumed token timestamp.
Uvicorn access-record arguments are filtered for credential-bearing route paths
without rewriting the ASGI request scope.

ASGI frame samples used the local TestClient, SQLite temp database, configured
synthetic roster, and a recorder around `http.response.start` plus the final
`http.response.body`. Ten invalid-parent samples ranged 101.549–103.593 ms
(median 103.054 ms); ten suppressed valid-invite samples ranged 101.476–103.306
ms (median 102.960 ms). Every observed sample exceeded 100 ms by about 1.5–3.6
ms; the branch medians differed by 0.094 ms. A separate blocked fake sender test
observed the final response body before sender dispatch. These small local
samples show no reproducible branch-correlated overrun, but they do not prove
equal timing under other database or host load. Capacity, delivery and ordinary
database paths that exceed the floor remain a release-time disclosure risk.
Browser journey and real mail were not run; neither is configured/authorized in
this task. Full `make check` is left to the committed-candidate fresh-check.
