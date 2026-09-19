# Client SaaS portal architecture (Phase 10A)

Scope: the customer-facing portal foundation for the Phase 10 route set in
`docs/KLYROW_CORPORATE_EMAIL_SAAS_COMPLETION_BLUEPRINT.md`, built on the
existing Vue 3 + Vite + TypeScript application in `apps/web`. Backend
behaviour, migrations, billing, providers and delivery activation are
untouched; every page is driven by existing browser (BFF) APIs or renders an
honest unavailable state.

## Composition

```
index.html → src/main.ts → routeManifest.browserRoute(pathname) → root view
                                          ├─ App (public auth)      unchanged
                                          ├─ Dashboard (/app)       unchanged
                                          ├─ Webmail (/app/mail)    unchanged
                                          ├─ Onboarding, Provisioning, AdminDashboard   unchanged
                                          └─ Portal (Phase 10 prefixes)  NEW
```

`src/routeManifest.ts` gains a `Portal` root for the product prefixes
(`/app/overview`, `/app/email`, `/app/content`, `/app/audience`,
`/app/campaigns`, `/app/journeys`, `/app/analytics`, `/app/deliverability`,
`/app/developer`, `/app/billing`, `/app/settings`, `/app/support`) and the
admin prefixes (`/admin/tenants`, `/admin/deliverability`, `/admin/abuse`,
`/admin/queues`, `/admin/reconciliation`, `/admin/billing`, `/admin/system`,
`/admin/audit`). Legacy roots keep their precedence and behaviour.

### Portal modules (`src/portal/`)

| Module | Responsibility |
| --- | --- |
| `routes.ts` | Typed route table: pattern, group, title, breadcrumb, audience, capability/roles, availability, dependency statement, consumed APIs, supported page states. `matchPortalRoute`, `groupIndexPath`, `safePortalPath`. |
| `access.ts` | `evaluateAccess(session, route, adminAuthority)` → granted / pending / signed-out / forbidden(reason). `visibleNavigation` derives the sidebar from the same decision; navigation is display only. |
| `adminAuthority.ts` | Server-proven platform-admin authority via `GET /app/api/admin/dashboard` (200 → proven, 403 → denied, else unknown). Cached per page load; reset on tenant switch. |
| `router.ts` | History-API navigation for portal paths, full navigation for legacy roots, unsafe destinations replaced by the overview. Same-origin link interception with `data-external` opt-out. No new dependency. |
| `errors.ts` | `normalizeFailure` turns `ApiError` (status, detail code, `X-Request-Id`, `X-Correlation-Id`) and transport errors into a `PortalFailure`; non-snake_case codes (for example credential-looking strings) are replaced by `request_failed_<status>`. `describeFailure` maps known codes to explanations. |
| `state.ts` | In-memory tenant-bound cache; `bindTenant` discards every entry when the organization changes; never uses browser storage. |
| `toasts.ts` | Notification queue rendered by `ToastRegion`. |
| `composables/usePage.ts` | Standard page lifecycle (loading → ready/empty, forbidden/error/session) with optional tenant cache and `stale` flag; `settle` for multi-source pages that degrade per source. |
| `components/` | Shell (TopBar, SideNav, Breadcrumbs, PageHeader, SkipLink, ToastRegion), states (Loading, Empty, Error, Forbidden, Unavailable, Degraded), dialogs (ModalDialog, ConfirmDialog), data (DataTable, PaginationControls, FilterBar, SearchInput, FormField, TabList, PanelCard, MetricCard, StatusBadge, SourceBadge, ActivityTimeline, SafeText), secrets (CopyButton, OneTimeSecret, RequestIdentifiers). |
| `pages/` | One component per implemented/partial page; `UnavailablePage` for every route without a browser API; `NotFoundPage`. `pages/index.ts` maps route names to components. |
| `portal.css` | Design tokens (dark enterprise identity, gold accent), shell layout, responsive rules (drawer ≤900px, compact top bar ≤600px, single-column ≤480px), reduced-motion support. |

### Session and authorization

- `requireSession()` (existing) gates every portal load; signed-out users are
  redirected to `/login?return_to=` with `safeReturnPath` (existing) rejecting
  external, protocol-relative, API and auth destinations.
- Capabilities come from the session body (`ROLE_PERMISSIONS` on the server);
  `hasCapability` (existing) treats `*` as all. Management-only pages use the
  membership role (`OWNER`/`ADMIN`).
- Platform-admin surfaces render only after the server proves authority. The
  admin shell (ADMIN brand marker, admin navigation) is never shown otherwise.
- Every API call still goes through `appApi`, which enforces same-origin
  `/app/api` and `/auth` paths, CSRF headers and 401 handling. Hidden
  navigation is never treated as authorization; forbidden routes render
  `ForbiddenState` without calling the page API.

### Organization switching and tenant separation

`POST /app/api/organizations/{tenant_id}/switch` rotates the server session.
The portal clears the in-memory tenant cache and performs a full navigation to
`/app/overview`, so no component state from the previous tenant survives.
`appApi` already invalidates the CSRF token and broadcasts a session change to
other tabs, which reload.

### Page state standard

Every page implements, where applicable: `loading` (skeleton with `role=status`),
`empty`, `ready`, `degraded` (per-source failure notice with request ID while
the rest renders), `forbidden` (server 403 or session decision), `unavailable`
(capability check failed: missing browser API) and `error` (normalized failure
with code, HTTP status, request and correlation identifiers and a retry
action). Cards on multi-source pages show a `SourceBadge`
(`live`, `stale`, `unavailable`, `not configured`, `forbidden`, `degraded`).

### Honest unavailability

A page is only shown as operational when its browser API exists in
`docs/api/runtime-routes.json`. The route table carries the missing contract
and `UnavailablePage` adds per-route disclosures (for example the incomplete
durable journey engine, disabled live payments, no fabricated progress or
metrics) plus links to supported alternatives. No placeholder data is ever
rendered.

### Secret handling

- No secret ever enters `localStorage`, `sessionStorage`, URLs, logs or
  analytics; the state cache is memory only and the Playwright suite asserts
  empty storage after credential flows.
- `OneTimeSecret` keeps the value in a local ref, masked by default, clears it
  on "I have stored it" and on unmount; `CopyButton` with `sensitive` never
  renders the value into the DOM.
- Error normalization discards response bodies and non-code details.
- API keys, service accounts, SMTP credentials and webhook secrets have no
  browser API today; their pages are unavailable and expose no controls.

### Safe rendering

All user- and provider-supplied values render through interpolation or
`SafeText`; `v-html` is not used anywhere in the portal (asserted by tests).

### Accessibility

Skip link to `main#kp-main`, landmark roles, labelled navigation regions
(`Product navigation`, `Platform administration`, `Breadcrumb`), visible focus
rings, keyboard-operable drawer, dialogs with focus trap, Escape and focus
restoration, roving tab index for tabs, `aria-current`, `aria-expanded`,
`role=status`/`alert` live regions, WCAG 2.1 AA colour contrast on the dark
theme. Axe runs at 360, 768, 1366 and 1920 widths in the Playwright suite.

### Responsive behaviour

≤900px: sidebar becomes a drawer with backdrop; ≤600px: compact top bar
(account initial, narrower switcher); ≤480px: brand text hidden, definition
lists stack. Tables scroll horizontally inside their wrapper; the page never
scrolls horizontally (asserted at 360px).

### Decisions recorded

- No `vue-router` dependency: the repository ships a path→root-view manifest
  and pinned dependency policy; a ~100-line history router avoids a new
  supply-chain surface and keeps the existing manifest as the entry contract.
- `ApiError` extends the existing error contract additively (message remains
  the detail code) so no existing test or caller changes.
- Existing views (`Dashboard.vue`, `Webmail.vue`, …) are untouched; the
  portal links to them rather than re-implementing them.
- `/v1/*` is not a browser dependency (Bearer-only); pages that need those
  capabilities are unavailable until a `/app/api` contract exists.
