# Operations

Use `scripts/start|stop|restart|health|update`. `update` refuses a dirty worktree. Prometheus scrapes gateway request/mail metrics; Grafana is loopback-only behind the protected app proxy. Monitor API rate/status, accepted/queued/sent/delivered counts, bounces, complaints, Postal queue/worker, all databases, RabbitMQ, container health, host node metrics, disk, webhook failures and TLS expiry.

Before enabling delivery: bootstrap Postal admin and organization/server; save API/SMTP credentials in `.env`; install Postal's signing key; configure Mautic DSN to Postal SMTP on the backend network; simulate bounces/unsubscribes; verify restore; publish DNS and PTR; test only controlled recipients. Keep `KLYROW_SAFE_MODE=true` until all gates pass.

Run `scripts/mail-readiness` for public A/MX/SPF/DKIM/DMARC/PTR, local SMTP exposure, STARTTLS, certificate, container health, disk, RabbitMQ, and database checks. Prometheus currently scrapes gateway lifecycle/request metrics and node-exporter host/disk metrics; Docker health checks cover Postal SMTP/worker/web, RabbitMQ, PostgreSQL, and both MariaDB instances. Alerting for queue depth, deferred mail, bounce/complaint rate, DNS/DKIM drift, external SMTP reachability, and mail-certificate expiry must be connected to the existing monitoring destination before launch; no competing stack has been created.

Incident response: suspend the tenant, revoke API keys, preserve audit/event logs, stop the Postal worker if mail must halt, rotate affected secrets, investigate, then resume gradually. Do not delete unrelated shared-server containers or alter global firewall policy.

Monitor request latency, statuses, profile/event growth, journey failures, consent revocations, suppression growth and deliverability alerts. The gateway listens on loopback and private `10.40.0.2:18000`; Postal SMTP remains on `127.0.0.1:2525` until DNS/PTR/TLS pass. Replace in-process rate limiting with a shared store before gateway scaling.

Synthetic HMAC/replay tests use `scripts/test-webhook` and `tests/production_safe_smoke.py`; they do not submit carrier mail. Klyrow sends signed outbound Middleware events only through the private mTLS edge at `https://middleware-email-events.internal.codestra.agency:18080`; `:8095` remains the Kong-to-Middleware internal upstream and is not a Klyrow outbound transport. Its n8n delivery and Odoo writes remain disabled pending approved credentials/workflow targets, and Klyrow never writes directly to Odoo PostgreSQL.
