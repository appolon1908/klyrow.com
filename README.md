# Klyrow Production Email Platform

## Repository authority

This repository is the **Klyrow email/SaaS backend and runtime authority**. It owns Postal/Mautic integration, the authenticated Klyrow API, tenant state, email/campaign submission, domain onboarding, suppressions, delivery events, billing foundations, runtime deployment and operational recovery.

`appolon1908-hue/klyrow-Website-` is the separate **public marketing website frontend**. It must not become a second Postal/Mautic backend, email queue, tenant database, provider credential store or authoritative delivery API. Website forms and customer-facing actions cross the governed Codestra Middleware boundary rather than writing directly to Klyrow internals, Odoo or n8n.

```text
Browser -> klyrow-Website- -> Kong/Middleware -> klyrow.com -> Postal/Mautic
```

Klyrow is an email/customer-communications platform. Contact-center voice remains VICIdial/Asterisk and SMS remains Telnexa/Jasmin; those are independent provider systems coordinated through Middleware.

Klyrow is a tenant-isolated email operations platform built around Mautic 7.1.3, Postal 3.3.7, and a public FastAPI gateway. It provides authenticated delivery submission, domain onboarding, RBAC, quotas, suppressions, webhook verification, an operator/client portal, metrics, backups, and a safe test mode.

The SaaS layer adds profiles/events, consent/preferences, nested behavioral segmentation, journey graphs and runs, deliverability snapshots, internal-event analytics, onboarding, TOTP MFA/session revocation, OpenAPI/idempotency/correlation, and safe foundations for experiments, AI providers, integrations and billing. See [SaaS P0](docs/SAAS_P0.md) and [API/webhooks](docs/API.md).

## Quick start

```bash
scripts/generate-env
scripts/deploy
scripts/health
```

Run the generator with `sudo`. It creates a root-owned `.env` and root-owned runtime secret files with mode 0600 without printing their values. Transfer the initial administrator credential through the approved secret-management path. Production submission remains disabled while `KLYROW_SAFE_MODE=true`. Do not change that setting until Postal is bootstrapped, DNS/PTR is complete, abuse handling is staffed, and the Postal server API key is installed through its configured `*_FILE` path.

Delivery remains forced safe unless the independent `KLYROW_PRODUCTION_GATE_APPROVED=true` control is also set after every launch gate is verified.

Public traffic is terminated by the existing host Nginx. The gateway, Mautic and Grafana bind only to `127.0.0.1`; databases and RabbitMQ have no host ports. Copy `docker/proxy/klyrow.conf` only after backing up and reviewing the shared Nginx configuration. The canonical host split and atomic authentication gate are documented in [browser edge and authentication](docs/EDGE_ROUTING_AUTH.md).

## Components

- `app.klyrow.com`: client/admin portal; Mautic under `/mautic`; Grafana under authenticated `/ops`.
- `api.klyrow.com`: Klyrow API, never raw Mautic/Postal admin APIs.
- `track.klyrow.com`: tracking ingress routed to the controlled gateway.
- `mail.klyrow.com`: SMTP identity and MX target.
- Mautic: contacts, templates, segments and campaign orchestration through Postal.
- Postal: SMTP/API submission, DKIM, queue workers and delivery events.
- PostgreSQL: Klyrow tenant/application state; MariaDB: isolated Mautic and Postal stores.

See [deployment report](DEPLOYMENT_REPORT.md), [architecture](docs/ARCHITECTURE.md), [operations](docs/OPERATIONS.md), and the [Codestra observability/Odoo integration contract](docs/OBSERVABILITY_INTEGRATION.md).

Production sending is intentionally launch-gated. See [DNS and deliverability](docs/DNS_AND_DELIVERABILITY.md), [SMTP](docs/SMTP.md), and [middleware integration](docs/MIDDLEWARE_INTEGRATION.md).
