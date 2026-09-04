# Procurement SSO-only canary v1

Status: implementation and isolated tests only. No production/canary deployment is
implied by this document. Preserve the existing 9206 container, production
Procurement, DMS v11, gateway 9599 and PR8 9201.

## Authority and identity

DAS owns personal passwords, module grants, blocking, password-reset policy,
session epochs and revocation. Procurement stores none of those credentials.
The adapter uses a client service secret only on the backchannel. Every protected
request introspects the module token; a revoked/reset epoch, changed subject,
missing grant, malformed authority response or authority outage fails closed.
There is no API-key bypass in SSO mode. Startup rejects incomplete SSO settings
and any simultaneously configured local administrator username/password hash.
Legacy mode is preserved only when all DAS_SSO_* settings are absent.

Suppliers/projects are a shared business registry within the granted module,
not private chat resources. Sessions, CSRF and audit actors are isolated by
authoritative DAS UUID. Body/query identity fields never establish identity.
Approval/confirmation/creator fields are rewritten from this trusted subject.
Read-only grants deny business writes and non-allowlisted GET routes on the backend.

## Configuration contract

Required runtime settings in a root-only environment file outside Git:

- `DAS_SSO_AUTHORIZE_URL`: browser URL ending `/access/sso/authorize/`.
- `DAS_SSO_INTERNAL_BASE_URL`: backchannel origin.
- `DAS_SSO_CLIENT_ID`: exactly `procurement`.
- `DAS_SSO_CLIENT_SECRET`: separate random service credential, at least 32 characters.
- `DAS_SSO_REDIRECT_URI`: exact registered URL ending `/auth/sso/callback`.
- `PROCUREMENT_AUTH_SECRET`: separate random state/session signing key, at least 32 characters.

Authorize and callback must have the same scheme and hostname; different ports
are allowed. Public URLs require HTTPS. HTTP is permitted only for loopback
canary URLs accessed through SSH. Cookie Secure is never disabled. Browser
acceptance must verify whether the selected loopback browser actually sends
Secure cookies; if not, report BLOCKED instead of weakening cookies or TLS.

This restriction is deliberate: DAS returns code/state using form_post, and a
SameSite=Lax state cookie cannot support cross-site POST callbacks. No code or
access token is placed into a redirect query. PKCE S256 is mandatory; state is
random and expires after 300 seconds. State and module cookies are signed,
HttpOnly, Secure and uniquely namespaced by the exact callback URI, including
its port. State cookie path is `/auth/sso/callback`; module cookie path is `/`.
The CSRF token is session-specific and rendered into authenticated HTML;
cookie writes require it plus the exact configured browser Origin.

Backchannel POSTs use `X-DAS-Client-Secret`, never URL credentials. Redirects
and environment proxies are disabled, TLS certificate verification is not
bypassed, timeout is 3 seconds and response size is capped. Module tokens last
at most 120 seconds and are renewed only after successful live introspection.
Tokens, cookies and passwords must not be included in diagnostics or logs.

Local `/auth/logout` clears the module cookie; it does not claim global
revocation. Central `/access/logout/` revokes its AccessSession and central
revoke-all revokes user sessions. The adapter observes those changes on the
next protected request. Previously copied bearer credentials are not locally
revoked by a cookie-clear operation; DAS remains the revocation authority.

## Prepared, not executed: isolated rollout

`docker-compose.sso-canary.yml` binds only `127.0.0.1:19206`, uses the NEW
`procurement-sso-canary-data-v1` volume and requires a coordinated existing
private canary network. A read-only `ss -ltnH sport = :19206` check found no
listener during preparation; repeat the check immediately before any deployment.
No source production volume or existing 9206 volume is referenced.

Before launch, coordinate DAS callback registration, browser tunnel ports,
service secret, identity-network connectivity and an immutable separately built
Procurement image. The compose `PROCUREMENT_SSO_CANARY_IMAGE` must be a verified
image content ID/digest, not an unverified mutable tag. The dedicated Dockerfile
takes a separately verified base reference and digest, plus source commit SHA;
do not overwrite existing Regional Mapping v2 or frozen DMS images.

Do not print an unsanitized `docker compose config`: env_file contains service
secrets. Only compare an allowlist of image/bind/volume/network/nonsecret URLs.

No existing state is migrated by this canary. Before any later stateful upgrade,
back up its dedicated volume and verify restoration first. Rollback is removal
from exposure/stopping only this new canary and retaining its volume; the
existing production and 9206 contours remain untouched. Never use `down -v` or
delete existing data as rollback.

## Cluster gates and sandbox outbox

Unassigned or conflicting project/lot clusters block matching, campaigns,
quote entry and comparison. A supplier must belong to that confirmed cluster;
stored cross-cluster quotations are not ranked. Legacy data is never relabelled
by these guards: any required regional migration remains separately approved.

New canary initialization adds three ledgers only: `campaign_requests`,
`outbox_approvals`, `sandbox_deliveries`. Request content fingerprints and an
optional client idempotency key prevent repeated/concurrent RFQ drafts. Reusing
a key for changed content is a conflict; matching pre-ledger campaigns require
review instead of automatic backfill or a duplicate.

Human approval binds the exact recipient, channel, content and cluster context.
Post-approval drift or legacy approval without that binding cannot be simulated.
`POST /api/outbox/{id}/simulate` is an explicit, CSRF/RBAC-protected local test;
Email/MAX/Telegram adapters contain no network transport or credentials. They
return a persistent idempotent receipt with `mode=sandbox`, `status=simulated`,
`external_send=false`. The business message remains `approved`, never `sent`.
No real message is dispatched, and no supplier is contacted. Changing workers
does not lose deduplication or approval state. Rollback retains all ledgers and
the dedicated canary volume; no production schema has been changed or applied.
