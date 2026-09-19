# Odoo agent-event handoff

Klyrow is downstream of Middleware for agent onboarding. It is not the Odoo
event ingress endpoint.

The system boundary is:

`Odoo outbox -> Middleware POST /api/v1/odoo/events -> durable inbox/ledger/outbox -> governed downstream handler -> Klyrow private email API`

The two public Odoo event types are:

- `codestra.odoo.agent.provisioning_requested`
- `codestra.odoo.agent.activation_email_requested`

Middleware authenticates the Odoo bearer token and signed envelope, validates
the event-specific safety contract, and durably accepts the event before a
downstream handler can issue a command.

## Activation-email boundary

The activation event carries the Keycloak execute-actions mode, the
`agent-welcome-v1` template key, the recipient, and a credential-free HTTPS
login URL with the required `UPDATE_PASSWORD` and `CONFIGURE_TOTP` actions.
It must not carry a password, token, secret, private key, recovery code, or
persisted action link.

Klyrow receives a private `email.message.send.v1` command only after the
downstream handler has completed its authorization, provider-readback, and
delivery-gate checks. Klyrow must not infer activation from a raw Odoo event or
call Keycloak on Odoo's behalf.

The email activation controls remain disabled by default:

- `KLYROW_SAFE_MODE=true`
- `KLYROW_PRODUCTION_GATE_APPROVED=false`
- `LIVE_EMAIL_DELIVERY=false`
- `EXTERNAL_EMAIL_DELIVERY=false`
- `PRODUCTION_PROVIDER_ROUTING=false`

Enabling the runtime profile is a separate operator action and is not part of
this repository change. Provider credential release, Odoo system parameters,
server environment changes, mail-server unarchiving, and end-to-end delivery
read-back remain required operational gates.

## Validation ownership

Klyrow tests read its checked-in
`codestra/integration/middleware-command-contract.v1.json` and verify disabled
email controls. Odoo event names, the upstream durable handoff, and activation
payload serialization require tests in their owning Odoo/Middleware repositories.
The document specifies that upstream contract; local fixtures are not evidence
that upstream serialization excludes credentials or that an event was delivered.
