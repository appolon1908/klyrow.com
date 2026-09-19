# Client SaaS portal — test evidence (Phase 10A)

- Canonical repository: `https://github.com/appolon1908-hue/klyrow.com`
- Base: `origin/main` at `fba9101416e0102f7d9b9f35c2256b85b8c18cf0`
- Branch: `phase/10a-client-saas-portal` (worktree `klyrow-client-portal`)
- Final commit SHA, PR and merge evidence: see the "Delivery" section at the end.

## Commands and results (Windows 11 workstation, Node 20.16, pnpm 10.32.1, Python 3.12.5)

All commands run from `apps/web` unless stated.

| Gate | Command | Result |
| --- | --- | --- |
| Install | `pnpm install --frozen-lockfile` | OK (lockfile unchanged; no new dependency) |
| Lint | `pnpm lint` | 0 errors; warnings are pre-existing in legacy views (`App.vue`, `Dashboard.vue`, `Webmail.vue`, …). `eslint src/portal src/Portal.vue e2e/portal.spec.ts` → 0 problems |
| Type check | `pnpm typecheck` (vue-tsc) | 0 errors |
| Unit / component (Vitest) | `pnpm test` | 14 files, 83 tests passed (22 pre-existing + 61 new) |
| Route manifest contract | `pnpm test:routes` | passed |
| Accessibility unit | `pnpm test:a11y` | passed |
| Production build | `pnpm build` | OK (`vue-tsc --noEmit && vite build`; ~184 kB JS / 52 kB CSS before gzip) |
| Playwright | `VITE_TEST_SERVER=1 pnpm vite build && node e2e/server.mjs` then `pnpm exec playwright test` | 32 passed (16 pre-existing `auth.spec.ts`/`session.spec.ts` + 16 new `portal.spec.ts`), Chromium |
| Python frontend contracts (repo root, `PYTHONPATH=. PYTHONUTF8=1`) | `pytest -q tests/test_browser_frontend_api_contract.py tests/test_webmail_source_authority.py tests/test_portal_contract.py tests/test_gateway_runtime_contract.py tests/test_auth_bff.py tests/test_reproducible_images.py tests/test_secret_scan_policy.py tests/test_edge_host_contract.py` | 45 passed (see environmental note 1 for the run without `PYTHONUTF8`) |
| Generated API artifacts | `python scripts/export-api-contracts.py --check` | exit 0 (no drift; no backend route changed) |
| API contract validation | `python scripts/validate-api-contracts.py` | 65 internal + 238 public operations validated |
| Event contracts | `python scripts/export-event-contracts.py --check` | exit 0 |
| Route matrix generator | `python apps/web/scripts/route-matrix.py` | regenerates `CLIENT_SAAS_ROUTE_MATRIX.md` (65 routes: 8 IMPLEMENTED, 12 PARTIAL, 41 MISSING, 4 BLOCKED) |
| Complete Python suite | `pytest -q tests --continue-on-collection-errors` | see "Complete Python suite" below |

### New Vitest coverage (`apps/web/src/__tests__/`)

| File | Covers |
| --- | --- |
| `portalRoutes.test.ts` | every Phase 10 route declared with metadata, parameter matching, API/legacy path rejection, group index resolution, safe in-app destinations, root manifest integration with legacy precedence |
| `portalAccess.test.ts` | session validity, capability and role guards, server-proven admin authority, navigation visibility per role, admin group isolation |
| `apiErrors.test.ts` | typed `ApiError` (status, code, request/correlation IDs, no body leakage), failure normalization, credential-looking codes replaced |
| `portalState.test.ts` | tenant-bound cache invalidation on organization change, no browser storage, admin authority probe (200/403/503), toasts, history vs hard navigation and unsafe URL fallback |
| `portalComponents.test.ts` | loading/empty/forbidden/unavailable/degraded/error states with request IDs, source badges, text-only rendering, modal focus trap/Escape/restore, confirm dialog, one-time secret display and copy control non-persistence, form field associations, tab keyboard model, table empty state |
| `portalShell.test.ts` | landmarks, skip link, role-filtered navigation, forbidden direct access without API call, unavailable state, admin isolation until proven, admin shell after proof, group prefix redirect, organization switch → cache cleared + hard reload, mobile toggle `aria-expanded`, not-found |
| `portalEmailPages.test.ts` | messages list/filter/search/paging/text-only, empty and error with IDs, message summary with honest timeline, domains list/claim/one-time DNS guidance/verify failure with ID, domain detail tabs, senders creation bound to verified domains, reader restrictions, inbound activation with confirmation |
| `portalProductPages.test.ts` | every unavailable route renders contract + disclosures with no mutating controls and no API call; journey/payment/SCIM/preference disclosures; analytics (no charts), deliverability, developer logs (redacted example), billing plan/usage (no payment actions), organization, team invitation one-time token, security session revocation with confirmation, admin counts pages |

### New Playwright coverage (`apps/web/e2e/portal.spec.ts`)

1. Signed-out user redirected from `/app/overview` with safe `return_to`.
2. Tenant user navigates permitted pages via the shell (history navigation, no reload, back button).
3. Tenant user cannot access `/admin/system`; no admin navigation.
4. Platform administrator reaches admin pages with the isolated admin navigation.
5. Organization switching reloads into the new tenant scope with fresh data and empty storage.
6. Webmail remains reachable and operational.
7. Domain list handles empty, ready and degraded (503 with request ID) states.
8. One-time credential display (invitation development token) never persists; API keys page shows unavailable with no secret controls.
9. SSO/SCIM honest unavailable states.
10. Billing pages expose no live payment actions or provider branding.
11. Mobile (360px) navigation by pointer and keyboard (skip link, toggle, Enter, Escape).
12. Error states display request/correlation IDs without secret data.
13–16. Axe WCAG 2.1 A/AA at 360×740, 768×1024, 1366×768, 1920×1080 on overview, domains, security and an unavailable page; no horizontal page scroll.

Playwright scenario 8 in the mission asked for API-key creation; API keys have no browser API (`/v1/api-keys` is Bearer-only), so the one-time display is exercised through the existing invitation development-token response and the API-keys page is asserted to be an honest unavailable state.

### Complete Python suite

`PYTHONPATH=. PYTHONUTF8=1 python -m pytest -q tests --continue-on-collection-errors`
(from the repository root, with the cp1252-only theme test deselected):
**1042 passed, 240 failed, 24 errors, 22 skipped in 9m11s**. Every failure and
error is a Windows platform incompatibility in backend tooling, none in
`apps/web`: 141 × `os.O_NOFOLLOW`, 37 × `os.O_DIRECTORY`, 48 × `os.geteuid`,
GPG-required Mautic backup tests, symlink privilege (`WinError 1314`),
`bash`/WSL execution, `signal.SIGWINCH`, `os.mkfifo`, `os.killpg`, and the
CRLF-affected `migrations/008` checksum. The remaining
`test_messaging.py::test_webhook_event_idempotency_and_delivery_retry_policy`
failure is the known baseline failure reported before this branch. The local
complete suite is therefore **not** classified as green; the Linux CI `test`
job is authoritative for it. Frontend-related Python contracts (45 tests) pass
locally.

## Environmental limitations (Windows workstation)

1. `tests/test_portal_contract.py::test_keycloak_theme_has_complete_localized_surfaces` fails only without `PYTHONUTF8=1` (`UnicodeDecodeError: 'charmap'` reading UTF-8 theme files with the cp1252 default). It passes with `PYTHONUTF8=1`; CI runs on Linux with UTF-8.
2. `tests/test_api.py` cannot be collected on Windows (`PermissionError` on a hard-coded `/tmp` path); unrelated to this change and already fixed on the owner's local `main` (`5262a1e`, not yet in `origin/main`).
3. `playwright.config.ts` starts the web server with `VITE_TEST_SERVER=1 pnpm vite build …`, which is POSIX shell syntax; on Windows the build and `node e2e/server.mjs` were started manually before `playwright test`. `e2e/server.mjs` needed `fileURLToPath` to serve `dist/` on Windows (included in this branch; no behaviour change on Linux).
4. The Playwright suite passed 5 of 6 consecutive local runs (32/32); one run reported a single failure whose artifact was overwritten before inspection. Treat CI as authoritative and inspect any recurrence rather than rerunning blindly.
5. Gitleaks is not installed locally; the diff was grepped for credential patterns (none) and CI's `secrets` job remains authoritative.

## Delivery

- Pushed branch: `phase/10a-client-saas-portal` (only this branch was pushed).
- Pull request: #136 — https://github.com/ingtrader21-spec/klyrow.com/pull/136
  (the canonical `appolon1908-hue/klyrow.com` URL redirects to
  `ingtrader21-spec/klyrow.com` after a repository transfer).
- PR HEAD at open: `43f2e927e56c50235197773a625212800a35e5e4`.
- CI on PR HEAD `43f2e92` (workflow run 35460449014): `contracts` pass (50s),
  `frontend` pass (1m31s: lint, typecheck, routes, Vitest, build, Playwright),
  `test` pass (5m44s: full Python suite on Linux), `secrets` pass (gitleaks, 9s),
  `image` pass (13m59s: reproducible OCI builds, Trivy, SBOM), `publish` skipped
  (PR); deploy-readiness `source-ci`/`secret-scan` pass, environment stages
  skipped as designed.
- Merge gate: `mergeable: MERGEABLE`, `mergeStateStatus: BLOCKED`,
  `reviewDecision: REVIEW_REQUIRED`. The `main` ruleset requires one approving
  review after the last push, resolved review threads and squash merge. The PR
  author cannot self-approve, so merge and post-merge verification are pending
  an independent reviewer.
