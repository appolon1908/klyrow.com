# PRO-S1 Organization & Identity — authority and contract freeze

Status: Gate A source freeze.

## Existing authority / reuse
- Keycloak owns human authentication, MFA, recovery and enterprise identity brokering. PAS-192/PAS-194 own runtime certification; this mission does not reimplement OIDC.
- Klyrow PostgreSQL owns Organization, TenantMember, TenantInvitation, tenant settings and browser-session projections.
- Existing browser contracts are retained: GET /app/api/context, organization switch, GET /app/api/team, POST /app/api/team/invitations, GET/DELETE /auth/sessions and POST /auth/logout-all.
- Existing Settings Organization, Team and Security pages are extended rather than replaced.

## Missing product contracts owned here
- GET /app/api/team/invitations — pending invitation list.
- DELETE /app/api/team/invitations/{invitation_id} — revoke pending invitation.
- PATCH /app/api/team/{user_id} — change member role.
- DELETE /app/api/team/{user_id} — deactivate membership.
- GET /app/api/identity/capabilities — honest enterprise SSO/SCIM capability/readiness projection.

SSO/SCIM configuration mutation remains unavailable until the canonical Keycloak/Middleware provisioning contract exists. UI must show the dependency rather than inventing direct Keycloak writes.

## Invariants
- OWNER membership cannot be removed or demoted if it would leave the tenant with zero active owners.
- A user cannot elevate a member above their own management authority.
- Invitation list/revoke and member mutation require management role + CSRF.
- Every mutation is tenant-scoped and audited.
- Identity/provider effects never bypass Caddy → Kong → Middleware → authorized adapter.
- Browser never receives Keycloak/admin secrets.

## UX
Organization = server-resolved org/membership context.
Team = active members + pending invitations + role change/removal/revoke.
Security = browser sessions; MFA/recovery are Keycloak-owned.
SSO/SCIM = capability/readiness surfaces until governed provisioning exists.
