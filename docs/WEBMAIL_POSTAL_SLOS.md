# Webmail / Postal production observability and SLOs

## Service boundary
Webmail browser/API -> durable Klyrow outbox -> Postal -> provider evidence, and Postal inbound -> verified Klyrow route -> tenant Inbox. Metrics and traces MUST NOT label tenant IDs, addresses, message IDs, subjects, bodies, tokens or raw URLs.

## SLOs
| SLI | Objective | Window |
|---|---:|---|
| Webmail/API successful requests | >= 99.9% | 30d |
| Accepted outbound not failed | >= 99.0% | 30d |
| Verified inbound successfully projected | >= 99.5% | 30d |
| Outbound queue age | p99 < 300s | 30d |
| Postal/provider interaction latency | p95 < 2s | 30d |
| Bounce ratio | < 5% | rolling 30m |
| Complaint ratio | < 0.2% | rolling 30m |
| Reconciliation unresolved | 0 sustained >10m | continuous |

## Trace contract
W3C trace context follows Webmail/API acceptance into the durable email command and is restored at Postal delivery. Inbound reconciliation creates/continues a bounded trace through route resolution and Inbox projection. OTLP export failure never changes mail acceptance or delivery state. Trace attributes exclude PII/secrets.

## Release gate
Dashboard and alerts are operational evidence, not permission to enable delivery. LIVE_EMAIL_DELIVERY, EXTERNAL_EMAIL_DELIVERY and PRODUCTION_PROVIDER_ROUTING remain false until separately approved.
