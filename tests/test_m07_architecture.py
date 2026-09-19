from pathlib import Path

from apps.gateway.app.business_events import KLYROW_EVENTS


ROOT = Path(__file__).parents[1]


def test_m07_migration_contains_exact_outbox_fencing_and_campaign_invariants():
    sql = (ROOT/"migrations/2026091302_m07_business_event_runtime.sql").read_text()
    for field in (
        "event_id", "event_type", "tenant_id", "payload", "payload_hash",
        "correlation_id", "trace_context", "attempt_count", "next_attempt_at",
        "lease_owner", "lease_expires_at", "created_at", "delivered_at", "last_error",
    ):
        assert field in sql
    for state in ("PENDING", "LEASED", "RETRYING", "DELIVERED", "DEAD_LETTER"):
        assert state in sql
    assert "uq_campaign_dispatch_recipient" in sql
    assert "campaign_id, campaign_version, recipient_hash" in sql
    assert "DROP TABLE" not in sql and "TRUNCATE" not in sql


def test_business_event_stream_contains_summaries_not_raw_telemetry_or_engagement():
    assert not any(token in event for event in KLYROW_EVENTS for token in (
        "prometheus", "metric.sample", "log", "trace", "open", "click",
    ))
    assert "klyrow.usage.daily" in KLYROW_EVENTS
    assert "klyrow.kpi.daily" in KLYROW_EVENTS
    assert "klyrow.campaign.summary" in KLYROW_EVENTS


def test_new_klyrow_runtime_has_no_direct_odoo_client_or_credentials():
    sources = "\n".join((ROOT/path).read_text().lower() for path in (
        "apps/gateway/app/business_events.py",
        "apps/gateway/app/business_event_worker.py",
        "apps/gateway/app/campaign_dispatcher.py",
        "apps/gateway/app/secret_responses.py",
    ))
    for forbidden in ("odoorpc", "xmlrpc.client", "odoo_password", "odoo_api_key", "odoo_url"):
        assert forbidden not in sources
    worker = (ROOT/"apps/gateway/app/business_event_worker.py").read_text()
    assert "/api/v1/events/klyrow" in worker
    assert "/internal/v1/events/klyrow" not in worker


def test_middleware_handoff_is_complete_and_campaign_activation_remains_controlled():
    handoff = (ROOT/"docs/integrations/M07_MIDDLEWARE_HANDOFF.md").read_text()
    for phrase in (
        "## Endpoint and transport", "## Event envelope", "## Event types and field schemas",
        "## Acceptance response", "## Retry and dead-letter semantics",
        "## Middleware inbox and deduplication", "## Odoo projection rules",
        "## Reconciliation", "## Outage acceptance requirements",
    ):
        assert phrase in handoff
    compose = (ROOT/"deploy/docker-compose.business-events.yml").read_text()
    assert "KLYROW_BUSINESS_EVENTS_ENABLED:-false" in compose
    assert "KLYROW_BUSINESS_EVENTS_HMAC_SECRET_FILE" in compose
    assert "KLYROW_CAMPAIGN_DISPATCHER_ENABLED:-false" in compose
    assert 'KLYROW_CAMPAIGN_DISPATCHER_ALLOW_LIVE: "false"' in compose
    assert "NOT AUTHORIZED" in handoff
