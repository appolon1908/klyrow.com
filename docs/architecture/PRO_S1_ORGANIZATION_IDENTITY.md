# PRO-S1 Organization & Identity — Gate A 01–05 Contract

Status: **FROZEN FOR REVIEW**. This document is the implementation authority for PAS-209. Gate B must not expand scope without updating this freeze.

## 01 — Current-state inventory

### Reuse
- Data: `Organization`, `TenantMember`, `TenantInvitation`, `TenantSetting`, `OidcIdentity`, Klyrow browser sessions.
- Browser APIs: `GET /app/api/context`, organization switch, `GET /app/api/team`, `POST /app/api/team/invitations`, session list/revoke/logout-all.
- UI: Organization, Team, Security, organization switcher.
- Identity runtime: Keycloak/OIDC. PAS-192/PAS-194 own runtime certification.

### Known gaps
- Pending invitation list/revoke.
- Member role update/removal.
- Professional SSO/SCIM readiness/configuration boundary.
- Complete state/error/permission contract for identity administration.

## 02 — Authority map

| Concern | Authority | Classification | Rule |
|---|---|---|---|
| Human authentication | Keycloak | REUSE | Keycloak owns human authentication; Klyrow never stores passwords or implements a second IdP. |
| MFA/recovery | Keycloak | REUSE | Product may display readiness/status; runtime remains Keycloak-owned. |
| Browser session | Klyrow BFF | REUSE/EXTEND | HttpOnly server session; no bearer token in browser storage. |
| Organization | Klyrow PostgreSQL | REUSE/EXTEND | Klyrow PostgreSQL owns tenant-scoped durable organization business truth. |
| Membership/role | Klyrow PostgreSQL | REUSE/EXTEND | Server-enforced capabilities. |
| Invitations | Klyrow PostgreSQL | REUSE/EXTEND | Single-use, expiring, revocable. |
| SSO/SCIM provisioning | Middleware → Keycloak adapter | NEW contract, not direct implementation | Browser cannot write Keycloak directly. |
| Cross-system audit/correlation | Middleware | REUSE | Correlation/request IDs across governed effects. |

Canonical path: **Client / Browser → Caddy → Kong → Middleware → authorized adapter → service/provider**.

## 03 — Logic, state and failure model

### Organization
States: `ACTIVE | SUSPENDED | DISABLED`.
- Only ACTIVE organizations may create new sessions/invitations.
- Switch requires active membership and enabled tenant.
- Suspended/disabled organization fails closed.

### Membership
States: `ACTIVE | INACTIVE`.
Roles: `OWNER | ADMIN | DEVELOPER | BILLING | SUPPORT | MARKETING | ANALYST | READ_ONLY`.
- OWNER/ADMIN may manage members/invitations.
- A tenant must always retain >=1 active OWNER.
- Last OWNER cannot be demoted or removed.
- Membership mutation is tenant-scoped, CSRF-protected and audited.
- Role/capability checks are server-side; hidden navigation is never authorization.
- Cross-tenant member IDs return not-found/denied without leaking membership existence.

### Invitation
States: `PENDING → ACCEPTED` or `PENDING → REVOKED`; expiry is terminal.
- Normalized email.
- Expiring, single-use token.
- Revoked/accepted/expired invitations cannot be reused.
- Invitation role is validated against canonical roles.
- Production never returns raw invitation token in normal browser response.

### Browser session
States: `ACTIVE → REVOKED/EXPIRED`.
- Organization switch rotates the session.
- Individual revoke and logout-all are authoritative immediately.
- Secure/HttpOnly/SameSite and CSRF boundaries remain existing auth authority.
- PAS-192 certifies real Keycloak runtime behavior.

### Enterprise identity
SSO readiness: `NOT_CONFIGURED | CONFIG_PENDING | CONFIGURED | DEGRADED`.
SCIM readiness: same state family.
- Product can read readiness safely.
- Configuration mutation is unavailable until Middleware has an authorized Keycloak provisioning adapter/command.
- No direct Keycloak admin API call from Klyrow browser/BFF.

### Failure rules
- 401: no valid browser identity/session.
- 403: authenticated but insufficient capability.
- 404: tenant-scoped resource unavailable; avoid cross-tenant existence leak.
- 409: invariant conflict, including `last_owner_protected`.
- 422: invalid role/input.
- 503: authoritative dependency unavailable; never silently downgrade security.

## 04 — API contract freeze

| Method | Path | Scope | State |
|---|---|---|---|
| GET | `/app/api/context` | authenticated | REUSE |
| POST | `/app/api/organizations/{tenant_id}/switch` | member + CSRF | REUSE |
| GET | `/app/api/team` | member | REUSE |
| POST | `/app/api/team/invitations` | OWNER/ADMIN + CSRF | REUSE |
| GET | `/app/api/team/invitations` | OWNER/ADMIN | EXTEND |
| DELETE | `/app/api/team/invitations/{invitation_id}` | OWNER/ADMIN + CSRF | EXTEND |
| PATCH | `/app/api/team/{user_id}` | OWNER/ADMIN + CSRF | EXTEND |
| DELETE | `/app/api/team/{user_id}` | OWNER/ADMIN + CSRF | EXTEND |
| GET | `/auth/sessions` | authenticated | REUSE |
| DELETE | `/auth/sessions/{session_id}` | authenticated + CSRF | REUSE |
| POST | `/auth/logout-all` | authenticated + CSRF | REUSE |
| GET | `/app/api/identity/capabilities` | OWNER/ADMIN | NEW read model |
| GET | `/app/api/identity/sso` | OWNER/ADMIN | FUTURE governed read model |
| PUT | `/app/api/identity/sso` | OWNER + fresh auth | BLOCKED until Middleware command exists |
| GET | `/app/api/identity/scim` | OWNER/ADMIN | FUTURE governed read model |
| POST | `/app/api/identity/scim/tokens` | OWNER + fresh auth | BLOCKED until Middleware command exists |
| DELETE | `/app/api/identity/scim/tokens/{id}` | OWNER + fresh auth | BLOCKED until Middleware command exists |

### API standard
- Same-origin BFF for browser.
- JSON typed schemas and stable error codes.
- Mutations require CSRF and idempotency where external effects exist.
- Lists define deterministic ordering and pagination before scale requires it.
- Request/correlation ID is returned/propagated for auditable operations.
- No secret, access token, SCIM token, Keycloak credential or raw provider payload in logs/errors.
- OpenAPI/Postman inventory must be regenerated during Gate B after contracts are implemented.

## 05 — Professional UX/design freeze

### Settings > Organization
Answers: What organization am I in? What is my role? What other organizations can I access?
- Organization identity/status.
- Current membership/role.
- Organization switcher.
- No destructive organization controls in PRO-S1.

### Settings > Team
Answers: Who has access? What can they do? Who is pending?
- Searchable member table: member, role, status, joined.
- Pending invitation section: email, role, expiry, revoke.
- Invite member modal.
- Role-change control with confirmation for privileged changes.
- Remove-member confirmation.
- Last-owner conflict shown as a clear protected-state explanation.

### Settings > Security
Answers: Where am I signed in? Can I revoke access?
- Active sessions.
- Current-session indicator.
- Revoke other session.
- Sign out everywhere.
- MFA/recovery links/status remain Keycloak-owned.

### Settings > SSO
Answers: Is enterprise SSO available/configured and who owns it?
- Authority/readiness status.
- Configuration summary when governed read contract exists.
- Until Middleware provisioning exists, display an explicit unavailable mutation state—not a fake form.

### Settings > SCIM
Answers: Is provisioning configured and healthy?
- Readiness/status.
- Token metadata only when governed contract exists; secrets are one-time reveal.
- No configuration mutation until Middleware provisioning contract exists.

### Required page states
Every page: loading, empty, ready, degraded, forbidden, error, stale/read-only where applicable.
Every action: idle, validating, submitting, success, conflict, forbidden, dependency unavailable.
Responsive: phone/tablet/desktop.
Accessibility: keyboard operation, visible focus, explicit labels, semantic tables/dialogs, live error/success notices.

## Gate A acceptance

Gate A passes only when:
- this authority map has no ownership collision with PAS-192/PAS-194/PAS-197/PAS-198;
- every endpoint is REUSE/EXTEND/NEW/BLOCKED;
- state/invariant/error rules are frozen;
- UI routes/states/actions are frozen;
- direct Keycloak/provider writes remain prohibited;
- PAS-209 contains exact branch/SHA and this document path.

Only then may PAS-210 execute steps 06–10.
