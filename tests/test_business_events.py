import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app import main
from apps.gateway.app.billing import UsageEvent
from apps.gateway.app.business_events import (
    BusinessEventOutbox,
    EventEnvelope,
    KLYROW_EVENTS,
    daily_snapshot,
    replay_dead_letter,
    snapshot_page,
)
from apps.gateway.app.business_event_worker import dispatch, operation_id_for_event


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for model in (main.Tenant, UsageEvent, BusinessEventOutbox):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "DB", factory)
    monkeypatch.setenv("KLYROW_BUSINESS_EVENTS_ENABLED", "true")
    with factory() as session:
        session.add_all([main.Tenant(id="a", name="A"), main.Tenant(id="b", name="B")])
        session.commit()
    yield factory
    engine.dispose()


def meter(session, tenant, key, quantity=1, day=None, unit="accepted_message"):
    day = day or datetime.now(timezone.utc).date() - timedelta(days=1)
    session.add(UsageEvent(
        id=key, tenant_id=tenant, subscription_id="sub", event_key=key,
        unit=unit, quantity=quantity, price_id="price",
        occurred_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
    ))


def configure_transport(monkeypatch, responses, requests):
    import apps.gateway.app.business_event_worker as worker
    monkeypatch.setenv("KLYROW_BUSINESS_EVENTS_URL", "https://middleware.test/api/v1/events/klyrow")
    monkeypatch.setattr(
        main,
        "runtime_secret",
        lambda name: {
            "KLYROW_BUSINESS_EVENTS_TOKEN": "fixture-service-identity",
            "KLYROW_BUSINESS_EVENTS_HMAC_SECRET": "fixture-hmac-secret-at-least-32-bytes",
        }[name],
    )
    real_client = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))


def test_all_m07_event_types_are_versioned_and_schema_validated():
    assert set(KLYROW_EVENTS) == {
        "klyrow.tenant.created", "klyrow.tenant.updated", "klyrow.subscription.changed",
        "klyrow.usage.daily", "klyrow.kpi.daily", "klyrow.campaign.summary",
        "klyrow.domain.status", "klyrow.provider.health", "klyrow.account.held",
        "klyrow.account.released",
    }
    with pytest.raises(ValidationError):
        EventEnvelope(id="evt_bad", type="klyrow.raw.click", tenant_id="a",
                      correlation_id="cor_a", occurred_at=datetime.now(timezone.utc), data={})
    with pytest.raises(ValidationError):
        EventEnvelope(id="evt_bad", type="klyrow.account.held", tenant_id="a",
                      correlation_id="cor_a", occurred_at=datetime.now(timezone.utc),
                      data={"status":"RELEASED","reason":"wrong","changed_at":datetime.now(timezone.utc)})


def test_wire_contract_requires_explicit_envelope_and_projection_defaults():
    common = {
        "id": "evt_required",
        "type": "klyrow.tenant.created",
        "tenant_id": "a",
        "correlation_id": "cor_required",
        "occurred_at": datetime.now(timezone.utc),
        "data": {"tenant_id": "a", "enabled": True},
    }
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(common)
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({**common, "version": 1, "source": "klyrow", "data": {"tenant_id": "a"}})
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({
            **common,
            "type": "klyrow.usage.daily",
            "version": 1,
            "source": "klyrow",
            "data": {
                "date": "2026-09-12",
                "quantity": 1,
                "snapshot_at": "2026-09-13T00:00:00Z",
            },
        })


def test_enabled_publisher_requires_endpoint_and_separate_credentials(
    monkeypatch,
):
    monkeypatch.setenv("KLYROW_BUSINESS_EVENTS_ENABLED", "true")
    monkeypatch.delenv("KLYROW_BUSINESS_EVENTS_URL", raising=False)
    with pytest.raises(ValueError, match="endpoint_required"):
        asyncio.run(dispatch())

    monkeypatch.setenv(
        "KLYROW_BUSINESS_EVENTS_URL",
        "https://middleware.test/api/v1/events/klyrow",
    )
    monkeypatch.setattr(main, "runtime_secret", lambda _name: "")
    with pytest.raises(ValueError, match="credentials_invalid"):
        asyncio.run(dispatch())


def test_usage_is_tenant_unit_and_utc_day_scoped_and_replayable(store):
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    with store() as session:
        meter(session, "a", "one", 3)
        meter(session, "b", "other-tenant", 99)
        meter(session, "a", "other-unit", 99, unit="storage")
        meter(session, "a", "other-day", 99, day=day-timedelta(days=1))
        first = daily_snapshot(session, "a", day)
        session.commit()
        original = first.payload
        assert json.loads(original)["data"]["quantity"] == 3
        assert daily_snapshot(session, "a", day).id == first.id
        meter(session, "a", "late", 2)
        revised = daily_snapshot(session, "a", day)
        session.commit()
        assert revised.id != first.id
        assert json.loads(revised.payload)["data"]["quantity"] == 5
        assert session.get(BusinessEventOutbox, first.id).payload == original


def test_snapshot_rollback_does_not_acknowledge_or_publish(store):
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    with store() as session:
        daily_snapshot(session, "a", day)
        session.rollback()
        assert session.scalar(select(BusinessEventOutbox)) is None
        assert snapshot_page(session, day, limit=1) == "a"
        session.commit()
        assert snapshot_page(session, day, after="a", limit=1) == "b"
        with pytest.raises(ValueError, match="closed"):
            daily_snapshot(session, "a", datetime.now(timezone.utc).date())


@pytest.mark.parametrize("response", [
    httpx.Response(503, json={"error": "offline"}),
    httpx.Response(429, headers={"Retry-After": "30"}),
    httpx.Response(408),
    httpx.Response(504),
])
def test_transient_failure_retries_then_strict_202_recovers(store, monkeypatch, response):
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    with store() as session:
        item = daily_snapshot(session, "a", day)
        session.commit()
        item_id, event_id, original = item.id, item.event_id, item.payload
    requests = []
    configure_transport(monkeypatch, [
        response,
        httpx.Response(202, headers={"Content-Type":"application/json"},
                       json={"operation_id":operation_id_for_event(event_id),"status":"ACCEPTED"}),
    ], requests)
    assert asyncio.run(dispatch()) == 0
    with store() as session:
        row = session.get(BusinessEventOutbox, item_id)
        assert row.state == "RETRYING" and row.payload == original
        if response.status_code == 429:
            assert row.next_attempt_at.replace(tzinfo=timezone.utc) >= datetime.now(timezone.utc) + timedelta(seconds=28)
        row.next_attempt_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        session.commit()
    assert asyncio.run(dispatch()) == 1
    assert requests[0].content == requests[1].content
    assert requests[0].headers["idempotency-key"].startswith("evt_")
    assert requests[0].headers["x-event-id"] == requests[0].headers["idempotency-key"]
    canonical = (
        f"{requests[0].headers['x-timestamp']}\n"
        f"{requests[0].headers['x-event-id']}\n"
        "klyrow\n"
    ).encode() + requests[0].content
    expected = hmac.new(
        b"fixture-hmac-secret-at-least-32-bytes",
        canonical,
        hashlib.sha256,
    ).hexdigest()
    assert requests[0].headers["x-signature"] == "sha256=" + expected
    with store() as session:
        assert session.get(BusinessEventOutbox, item_id).state == "DELIVERED"


@pytest.mark.parametrize("response,error", [
    (httpx.Response(200, text="<html>login</html>"), "middleware_http_200"),
    (httpx.Response(202, text="not json"), "middleware_202_content_type_invalid"),
    (httpx.Response(202, headers={"Content-Type":"application/json"}, json={"status":"ACCEPTED"}), "middleware_202_body_invalid"),
    (httpx.Response(202, headers={"Content-Type":"application/json"}, json={"operation_id":"op_1","status":"WRONG"}), "middleware_202_body_invalid"),
    (httpx.Response(302, headers={"Location":"https://elsewhere.invalid"}), "middleware_http_302"),
    (httpx.Response(409), "middleware_http_409"),
    (httpx.Response(400), "middleware_http_400"),
    (httpx.Response(401), "middleware_http_401"),
    (httpx.Response(403), "middleware_http_403"),
    (httpx.Response(404), "middleware_http_404"),
])
def test_non_acceptance_is_durable_permanent_failure(store, monkeypatch, response, error):
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    with store() as session:
        item = daily_snapshot(session, "a", day)
        session.commit()
        item_id, original = item.id, item.payload
    configure_transport(monkeypatch, [response], [])
    assert asyncio.run(dispatch()) == 0
    with store() as session:
        row = session.get(BusinessEventOutbox, item_id)
        assert row.state == "DEAD_LETTER"
        assert row.payload == original
        assert row.last_error == error


def test_expired_last_attempt_goes_to_dlq_without_loss(store, monkeypatch):
    day = datetime.now(timezone.utc).date()-timedelta(days=1)
    with store() as session:
        row = daily_snapshot(session, "a", day)
        row.state = "LEASED"
        row.attempt_count = 8
        row.lease_owner = "old-worker"
        row.lease_expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        session.commit()
        item_id = row.id
    monkeypatch.setenv("KLYROW_BUSINESS_EVENTS_URL", "https://middleware.test/api/v1/events/klyrow")
    monkeypatch.setattr(main, "runtime_secret", lambda _: "fixture-hmac-secret-at-least-32-bytes")
    assert asyncio.run(dispatch()) == 0
    with store() as session:
        row = session.get(BusinessEventOutbox, item_id)
        assert row.state == "DEAD_LETTER" and row.payload


def test_dead_letter_replay_is_integrity_checked_and_restart_safe(store):
    day = datetime.now(timezone.utc).date()-timedelta(days=1)
    with store() as session:
        row = daily_snapshot(session, "a", day)
        row.state = "DEAD_LETTER"
        row.attempt_count = 8
        session.commit()
        replay_dead_letter(session, row.event_id, tenant_id="a")
        assert row.state == "RETRYING" and row.attempt_count == 0
        session.commit()
        with pytest.raises(ValueError, match="not_dead_letter"):
            replay_dead_letter(session, row.event_id, tenant_id="a")


def test_stale_worker_cannot_complete_another_workers_lease(store, monkeypatch):
    day = datetime.now(timezone.utc).date()-timedelta(days=1)
    with store() as session:
        row = daily_snapshot(session, "a", day)
        session.commit()
        item_id, event_id = row.id, row.event_id
    def takeover(_request):
        with store() as session:
            row = session.get(BusinessEventOutbox, item_id)
            row.attempt_count += 1
            row.lease_owner = "new-owner"
            row.lease_expires_at = datetime.now(timezone.utc)+timedelta(minutes=2)
            session.commit()
        return httpx.Response(202, headers={"Content-Type":"application/json"},
                              json={"operation_id":operation_id_for_event(event_id),"status":"ACCEPTED"})
    import apps.gateway.app.business_event_worker as worker
    monkeypatch.setenv("KLYROW_BUSINESS_EVENTS_URL", "https://middleware.test/api/v1/events/klyrow")
    monkeypatch.setattr(main, "runtime_secret", lambda _: "fixture-hmac-secret-at-least-32-bytes")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(takeover), **kwargs))
    assert asyncio.run(dispatch()) == 0
    with store() as session:
        row = session.get(BusinessEventOutbox, item_id)
        assert row.state == "LEASED" and row.attempt_count == 2 and row.lease_owner == "new-owner"


def test_correlation_and_stored_trace_context_propagate(store, monkeypatch):
    import apps.gateway.app.business_events as events
    monkeypatch.setattr(events, "trace_carrier", lambda: {
        "traceparent":"00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "tracestate":"vendor=value",
        "authorization":"must-not-propagate",
    })
    day = datetime.now(timezone.utc).date()-timedelta(days=1)
    with store() as session:
        row = daily_snapshot(session, "a", day)
        session.commit()
        correlation_id, event_id = row.correlation_id, row.event_id
    requests = []
    configure_transport(monkeypatch, [httpx.Response(
        202, headers={"Content-Type":"application/json"},
        json={"operation_id":operation_id_for_event(event_id),"status":"ACCEPTED"},
    )], requests)
    assert asyncio.run(dispatch()) == 1
    assert requests[0].headers["x-correlation-id"] == correlation_id
    assert requests[0].headers["traceparent"].startswith("00-0123456789abcdef")
    assert requests[0].headers["tracestate"] == "vendor=value"
    assert "must-not-propagate" not in str(requests[0].headers)


def test_acknowledgement_is_bound_to_the_exact_event(store, monkeypatch):
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    with store() as session:
        row = daily_snapshot(session, "a", day)
        session.commit()
        item_id = row.id
    configure_transport(
        monkeypatch,
        [
            httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                json={
                    "operation_id": "op_00000000000000000000000000000000",
                    "status": "ACCEPTED",
                },
            )
        ],
        [],
    )
    assert asyncio.run(dispatch()) == 0
    with store() as session:
        row = session.get(BusinessEventOutbox, item_id)
        assert row.state == "DEAD_LETTER"
        assert row.last_error == "middleware_202_operation_id_mismatch"


def test_network_failure_is_retryable_and_tenant_replay_isolated(store, monkeypatch):
    day = datetime.now(timezone.utc).date()-timedelta(days=1)
    with store() as session:
        row = daily_snapshot(session, "a", day)
        session.commit()
        event_id = row.event_id
    configure_transport(monkeypatch, [httpx.ConnectError("offline")], [])
    assert asyncio.run(dispatch()) == 0
    with store() as session:
        row = session.scalar(select(BusinessEventOutbox).where(BusinessEventOutbox.event_id == event_id))
        assert row.state == "RETRYING" and row.last_error == "middleware_network_error"
        row.state = "DEAD_LETTER"
        session.commit()
        with pytest.raises(ValueError, match="not_found"):
            replay_dead_letter(session, event_id, tenant_id="b")
