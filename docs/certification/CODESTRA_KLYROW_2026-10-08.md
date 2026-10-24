# Codestra / Klyrow end-to-end certification checkpoint — 2026-10-08

**Decision: PARTIAL PASS; production GO = NO.**

This document records observed results, not an assertion that all UI routes, email-provider effects, billing, or live identity journeys are ready.

## Source and deployment authority

- Repository: `appolon1908/klyrow.com`.
- Frontend/API UX PR: #188, stacked on SES feedback #187, SES transport #186 and base/build #185. These remain separate from protected `main`.
- PR #188 exact tested source commit: `2086b7cfffe685dae3189ccde3852b6d88b84256`.
- Exact-source Docker build completed on Server 3; image ID `sha256:2187b9177d66a0a44a6ba4f6a8f2774140cade887445ea55ef262d8d2ac348a5`.
- Private PR #188 UX preview on Server 3: `10.0.0.218:18082`, health and new branded favicon HTTP 200. Preview is **not** the active public gateway.
- The active public gateway is the separate PR #189 legal-page hotfix image, backed by PostgreSQL; the live browser entry point is `https://app.klyrow.com`.
- Historical handoff on Server 3: `/srv/codestra/deployments/klyrow/FRONTEND_BACKEND_UX_CERTIFICATION_2026-10-08.md`.

## Confirmed functional tests

| Scope | Result and boundary |
|---|---|
| GoDaddy DNS | Canonical `app.klyrow.com` A record moved from unresponsive `37.27.128.39` to gateway `179.52.246.125`; authoritative nameservers checked |
| TLS and routing | Trusted HTTPS for `app.klyrow.com`, root `klyrow.com` browser navigation redirects to canonical hostname, API health preserved |
| Public frontend | Login, signup, forgot-password, Terms, Privacy returned HTTP 200 through the public gateway |
| External independently verified | Separate Boston Windows host returned HTTP 200 for login, signup, Terms, Privacy, Klyrow API health, Keycloak OIDC discovery, and Codestra homepage |
| Keycloak integration | OIDC discovery endpoint returned HTTP 200; `/auth/login` issues the expected browser redirect, but **no authenticated callback or user session has been certified** |
| Browser responsive/interaction | Prior audit recorded 9/9 desktop/mobile interactions: password visibility, language, recovery navigation, signup, field validation and focus; no JS exceptions or horizontal overflow for tested views |
| Route inventory | 72 portal routes: 26 labelled implemented, 12 partial and 34 deliberately unavailable. Unavailable routes must not be advertised as working |
| Frontend/backend API contracts | 61/61 unique declared API methods used by implemented/partial routes matched composed FastAPI OpenAPI; 387 paths and 442 operations reported by inventory |
| Unauthorized access | Previous certification recorded 46/46 protected GET endpoints returning 401 with no browser session |
| Exact GitHub PR #188 Python parity tests | 2/2 passed in a clean worktree; worktree clean |
| Exact GitHub PR #188 image | Local Docker build passed and revision label matched the PR source SHA |
| Odoo / Codestra independent health | Odoo login, Codestra frontend, and Klyrow health HTTP 200 on Server 3, without certifying all CRM workflows |
| Terms/Privacy legal authority | Public links now resolve, but their informational content is **not evidence of legal review or binding approved policies** |
| Favicon | PR #188 preview favicon HTTP 200; current public PR #189 gateway returned 404 for `/auth-assets/favicon.svg`, pending source convergence |

## Amazon SES and feedback

- Amazon SES has 14 verified identities with DKIM success in `us-east-1`; 42 DKIM records were deployed to GoDaddy.
- Custom MAIL FROM MX and SPF records were published for 14 domains and checked on both authoritative nameservers. AWS SES's last confirmed API readback still reported `PENDING` (do not infer AWS completion from DNS publication).
- SES simulator acceptance and SNS/SQS Send/Delivery event transport were observed; PR #187's feedback worker remains opt-in and is **not enabled against the AWS queue**.
- SMTP credentials were transferred host-to-host to Server 3, but protected local import, SMTP AUTH and end-to-end customer delivery were **not** certified. Live email sending remains disabled.

## Current CI and release blockers

1. PRs #185–#189 require their own required CI, CODEOWNERS review and promotion approval; do not merge or override policy solely on local builds.
2. Gateway CI for PR #188 reported FAIL: inherited Python full-suite regressions and high-severity frontend dependency audit findings. PR #189 had an additional generated API inventory drift; its three generated files were regenerated and pushed; the new GitHub CI result must be checked.
3. A separate Server 3 `ci-repair-worktree` contains other-agent uncommitted changes. It was **not** reset, force-pushed or overwritten.
4. Full Keycloak browser login/callback, authenticated tenant navigation, 34 unavailable routes, 12 partial routes, and provider delivery remain unverified or incomplete.
5. Production favicon convergence between PR #188 preview and PR #189 live image is still required.
6. No live email sending, queue-deletion activation or privileged production changes are authorized by this document.

## Reproducible checks

- Browser API contract tests: `pytest -q tests/test_portal_backend_contract_parity.py`.
- Server-3 public endpoints: `/readyz`, `/v1/health`, `/login`, `/signup`, `/terms`, `/privacy`.
- Canonical production auth: `https://app.klyrow.com/auth/login` (302 to the registered Keycloak realm); requires a **separate authorized browser sign-in test** before acceptance.
- Verify required GitHub Actions on the **new latest PR SHA**, not earlier intermediate commits.

No access keys, SMTP credentials, secret contents or session tokens are included in this report.
