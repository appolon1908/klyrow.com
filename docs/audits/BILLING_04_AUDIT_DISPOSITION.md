# Billing Mission 04 Audit Disposition

Source audit: `LOGIC_ENDPOINT_REVIEW_2026-09-16.md`
Base reviewed: `3811969243f1af1b5bcf425b6e71811d0831dee8`

## P1 Billing Authorization

- AUDIT_FINDING: Billing mutations did not consistently require billing authority; capability resolution omitted billing mutation permissions.
- CURRENT_MAIN_STATUS: Subscription, usage, invoice, payment-method, payment, refund, checkout, plan-change, credit-note, and wallet mutation paths used generic authentication or inline role checks.
- FIX_REQUIRED: Centralize `billing.read` and `billing.manage` authorization and use it for tenant billing mutations. Keep catalog, manual confirmation, and administrative corrections platform-admin-only.
- SUPERSEDED: No.
- EXTERNAL_OWNED: No.
- MISSION_04_ACTION: Added canonical billing authorization helpers and routed tenant billing mutations through the shared helper. Browser checkout uses CSRF plus `billing.manage`; browser reads use `billing.read`.
- TEST_EVIDENCE: `tests/test_billing.py` and `tests/test_billing_browser.py` pass. Broader authorization coverage remains required.

## P1 Sandbox Settlement Authority

- AUDIT_FINDING: SANDBOX payment and checkout paths could confirm payments and activate commercial state in the authoritative tables without a server-controlled hosted-checkout/provider-evidence boundary.
- CURRENT_MAIN_STATUS: `POST /v1/billing/payments` called `post_settlement()` for `provider=SANDBOX`; legacy checkout marked SANDBOX sessions completed and activated subscriptions.
- FIX_REQUIRED: Prevent legacy sandbox shortcuts from creating confirmed financial truth or treating checkout as success. Route hosted Stripe checkout through a single application service and durable provider evidence.
- SUPERSEDED: No.
- EXTERNAL_OWNED: No.
- MISSION_04_ACTION: Legacy SANDBOX payment now returns `sandbox_payment_requires_hosted_checkout`; legacy SANDBOX checkout returns `sandbox_checkout_requires_hosted_checkout`. Added `billing_checkout.create_or_resume_stripe_checkout` with stable attempt-derived Stripe idempotency and split transactions.
- TEST_EVIDENCE: `tests/test_billing.py`, `tests/test_billing_ledger.py`, `tests/test_payment_attempts.py`, and Stripe/provider worker tests pass. Full concurrency and live PostgreSQL verification are still pending.

## P1 Invoice Financial Status

- AUDIT_FINDING: Invoice status did not consistently reflect net outstanding balance; confirmed payments, refunds, and credits were not uniformly derived from one authority.
- CURRENT_MAIN_STATUS: `billing_ledger.invoice_balance()` existed, but PaymentAttempt balance code independently subtracted captured attempts and browser summaries maintained separate formulas.
- FIX_REQUIRED: Make the payment ledger the sole financial authority and ensure PaymentAttempt lifecycle does not affect amount paid, amount due, or invoice status by itself.
- SUPERSEDED: No.
- EXTERNAL_OWNED: No.
- MISSION_04_ACTION: PaymentAttempt eligibility now delegates to `billing_ledger.invoice_balance()` and no longer subtracts captured attempts. Capture requires one exact confirmed Payment linked by tenant, invoice, currency, amount, and attempt identity.
- TEST_EVIDENCE: Focused authority suite: `82 passed`, including capture rejection without confirmed payment and ledger replay behavior. Browser summary and reconciliation formula convergence remain follow-up work.

## Scope Limits

The source audit's other findings concern invitation routing, mail delivery policy, credential stores, subscription billability, profile merging, suppression precedence, campaign delivery reporting, and Postal provisioning. They are not billing findings and are not relabeled as Mission 04 work.

No deployment, production Stripe configuration, production key, or real-money operation was used.
