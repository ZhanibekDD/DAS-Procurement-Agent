# Durable module logout after the browser race

## Observed failure

Real Chrome testing of `d2de3b956ea419cedcc8abe83ce9564ac9bb1e80` showed one failure
in three login/logout cycles: native module logout returned 303, a late background
API response restored the module cookie, and `/api/auth/session` returned 200.
The form Origin was correct. UI startup issues parallel GETs; each request performed
DAS introspection and emitted a refreshed cookie. Logout deleted only the browser
cookie and did not revoke the stable module login identity server-side.

## Scope of the fix

- Reuse the cryptographically random, signed per-login CSRF nonce as the stable
  module-session identity. Derive an HMAC-SHA256 revocation key from module client,
  exact callback scope, authenticated subject and nonce. Token refresh preserves
  this identity; another user, module, or fresh login has a different identity.
- Add the small persistent SQLite `sso_module_session_revocations` table containing
  only opaque hash, absolute expiry and revocation time. No token, JWT, raw nonce,
  user password, or central-session credentials are stored there.
- Logout inserts its revocation and audit event in one transaction. It remains local
  to this Procurement login; it does not call central DAS logout/revoke endpoints.
- Every protected request validates revocation before and after introspection, and
  immediately before setting a refreshed cookie. Storage failure is 503, not access
  or a false successful logout. Revoked sessions return 401 and clear stale cookies.
- A response already dispatched on the network can physically arrive late. It still
  cannot restore authorization: any old or rotated cookie with that revoked session
  identity fails server-side. Revocation is not implemented as a process-local cache.
- Existing Origin/CSRF checks, Secure/HttpOnly/SameSite=Lax and form-only
  `strict-origin` policy are unchanged.

## Bounded lifetime and additive migration

A new signed `session_exp` is set at login using the existing configured
`session_ttl_seconds` (validated range 5 minutes to 24 hours). Refresh preserves the
absolute deadline. The short-lived cookie expires at the earlier of token expiry
and that deadline. A session cannot slide forever and a delayed response cannot
mint a valid cookie after the deadline. Pre-fix module cookies missing this claim
fail closed with 401; the user signs in through DAS again. The central DAS session
is not revoked, and standalone legacy username/password sessions are unchanged.

Normal `Database.initialize()` creates the new table/index idempotently. There is
no business data backfill or change to suppliers/projects/lots. Tests emulate an
older schema and a fresh Database instance to verify additive migration and retained
revocation. Deployment and production schema changes are **not** part of this patch.

`Database.cleanup_sso_revocations(now=<current UTC Unix time>, limit=1000,
dry_run=True)` is an explicit maintenance primitive, not a public endpoint or an
automatic task. It defaults to counting at most 1000 expired tombstones. Applying
it requires `dry_run=False` and deletes only rows whose absolute deadline has
passed, in one transaction. Use the actual current clock, never a future timestamp.
No cleanup was run against any live database. After the deadline, the signed cookie
is invalid even if its tombstone has been removed. The table contains no secrets.

## Verification and rollback limits

Synthetic regression tests coordinate real threads at three race points: slow
introspection, slow endpoint, and cookie creation. Logout commits first, late
responses return 401, and previously issued/rotated cookies cannot access the API.
Tests also cover fresh login, other user's/other login's isolation, module scope,
storage outage, restart persistence, expiry, bounded dry-run/apply cleanup and
idempotent revocation. Real Chrome retesting is separate evidence.

Take the existing Online Backup before any separately authorized canary deployment.
Older images can read the business schema but **do not enforce this new revocation
table**. A security rollback must therefore invalidate module cookies (rotate only
the module signing secret under separate authorization) or keep Procurement access
closed until a safe image is restored. Do not advertise an old image as logout-safe,
do not drop revocation records as routine rollback, and do not revoke all DAS sessions.
