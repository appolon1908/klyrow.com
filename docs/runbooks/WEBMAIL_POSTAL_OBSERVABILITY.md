# Webmail / Postal observability runbook

1. Confirm gateway /healthz, /readyz and private /metrics. Never expose /metrics publicly.
2. Check queue age/depth and DEAD_LETTER/RETRY/INDETERMINATE states before replay.
3. For inbound failures, verify domain/provider-domain status, exact inbound route, Postal evidence authentication and idempotent Inbox projection.
4. For send failures, inspect durable outbox state and provider evidence; do not create a second send while an attempt is indeterminate.
5. For latency, separate Klyrow gateway latency from Postal/provider latency and check private network/collector health.
6. For bounce/complaint alerts, stop affected marketing dispatch through the governed gate; do not suppress transactional mail except via the existing hard-suppression policy.
7. For reconciliation failures, reconcile before retry; preserve provider_message_id and idempotency identity.
8. Use correlation/trace IDs only. Never paste recipient addresses, bodies, tokens or secrets into alerts/logs.
9. After recovery, verify alert resolves, queue age returns below SLO, trace continuity is visible in Tempo, and no duplicate Inbox/Sent projection occurred.
