# Client SaaS portal rollback (Phase 10A)

The portal is frontend-only. It adds no migration, no backend route, no
environment flag and no deployment setting. Live email delivery and billing
activation flags are unchanged.

## Instant rollback (no redeploy)

Portal routes are additive. If a portal page misbehaves, users can continue on
the legacy roots, which are unchanged:

- `/app` — legacy command center (send, messages, domains, team)
- `/app/mail` — webmail
- `/onboarding`, `/app/provisioning`, `/admin`, `/admin/provisioning`

## Source rollback

1. Revert the Phase 10A commits on `main` (they are frontend-only:
   `apps/web/**` and `docs/frontend/**`), or redeploy the previous approved
   immutable frontend image through the normal promotion process.
2. Rebuild the frontend candidate; the route manifest returns to the previous
   root views (`Portal` root and `src/portal/**` disappear).
3. Verify: `pnpm --prefix apps/web test:routes`, `pnpm --prefix apps/web test`,
   `pnpm --prefix apps/web build`, and the existing Playwright
   `e2e/session.spec.ts` for `/app` and `/app/mail`.

## Data considerations

None. The portal writes nothing to browser storage and creates no server-side
state beyond the existing browser API actions (domain claim, sender creation,
invitation, session revocation) that already exist in the legacy views and
public API. Nothing needs to be deleted or migrated on rollback.

## Partial rollback

To hide a single page while keeping the shell, set its `availability` to
`unavailable` in `apps/web/src/portal/routes.ts` and remove it from
`apps/web/src/portal/pages/index.ts`; the route then renders the honest
unavailable state. Re-run `pnpm --prefix apps/web test` and regenerate
`docs/frontend/CLIENT_SAAS_ROUTE_MATRIX.md` with
`python apps/web/scripts/route-matrix.py`.
