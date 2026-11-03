import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.gateway.app import main, provider
from apps.gateway.app.provider_outbox_delivery import dispatch, status


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    main.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "DB", factory)
    with factory() as session:
        session.add_all([main.Tenant(id="a", name="A"), main.Tenant(id="b", name="B")])
        session.commit()
    yield factory
    engine.dispose()


def event(identifier, payload=None, **kwargs):
    return provider.ProviderEvent(
        id=identifier,
        tenant_id="a",
        message_id="message",
        kind="message.delivered",
        payload_json=json.dumps({}) if payload is None else payload,
        **kwargs,
    )


def usage(identifier="usage", **kwargs):
    return provider.ProviderUsageEvent(
        id=identifier,
        tenant_id="a",
        message_id=identifier,
        stream="TRANSACTIONAL",
        result_category="DELIVERED",
        **kwargs,
    )


@pytest.mark.parametrize("bad_payload", ["{", "[]", "null", '"text"'])
def test_bad_event_does_not_starve_valid_events_or_usage(database, monkeypatch, bad_payload):
    with database() as session:
        session.add_all([event("bad", bad_payload), event("good"), usage()])
        session.commit()
    emit = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "emit_middleware", emit)
    result = asyncio.run(dispatch())
    assert result == {
        "events_delivered": 1,
        "events_failed": 1,
        "usage_delivered": 1,
        "usage_failed": 0,
    }
    with database() as session:
        bad = session.get(provider.ProviderEvent, "bad")
        assert (bad.state, bad.attempts, bad.last_error) == (
            "RETRY",
            1,
            "invalid_provider_event_payload",
        )
    assert {call.args[1]["event_id"] for call in emit.await_args_list} == {"good", "usage"}


def test_transport_exception_isolated_and_attempts_bounded(database, monkeypatch):
    with database() as session:
        session.add_all([event("bad", attempts=7), event("good"), usage()])
        session.commit()

    async def emit(kind, payload):
        if payload["event_id"] == "bad":
            raise RuntimeError("never expose this text")
        return True

    monkeypatch.setattr(main, "emit_middleware", emit)
    result = asyncio.run(dispatch())
    assert result["events_delivered"] == result["usage_delivered"] == 1
    with database() as session:
        bad = session.get(provider.ProviderEvent, "bad")
        assert (bad.state, bad.attempts, bad.last_error) == (
            "DEAD_LETTER",
            8,
            "middleware_delivery_exception",
        )


@pytest.mark.parametrize("identifier", [None, ""])
def test_empty_legacy_event_id_uses_stable_outbox_identity(database, monkeypatch, identifier):
    with database() as session:
        session.add(event("stable", json.dumps({"event_id": identifier})))
        session.commit()
    emit = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "emit_middleware", emit)
    for _ in range(2):
        asyncio.run(dispatch())
        with database() as session:
            session.get(provider.ProviderEvent, "stable").available_at = provider.now()
            session.commit()
    assert [call.args[1]["event_id"] for call in emit.await_args_list] == ["stable", "stable"]


def test_usage_claim_excludes_overlapping_worker_and_recovers_expiry(database, monkeypatch):
    with database() as session:
        session.add(usage(state="PROCESSING", available_at=provider.now() - timedelta(seconds=1)))
        session.add(
            usage("future", state="PROCESSING", available_at=provider.now() + timedelta(minutes=4))
        )
        session.commit()
    calls = []

    async def emit(kind, payload):
        calls.append(payload["event_id"])
        assert (await dispatch())["usage_delivered"] == 0
        return True

    monkeypatch.setattr(main, "emit_middleware", emit)
    assert asyncio.run(dispatch())["usage_delivered"] == 1
    assert calls == ["usage"]


@pytest.mark.parametrize(
    "model,is_event", [(provider.ProviderEvent, True), (provider.ProviderUsageEvent, False)]
)
def test_old_claim_cannot_acknowledge_new_owner(database, monkeypatch, model, is_event):
    with database() as session:
        session.add(event("item") if is_event else usage("item"))
        session.commit()

    async def emit(kind, payload):
        with database() as session:
            item = session.get(model, "item")
            if is_event:
                item.updated_at += timedelta(seconds=1)
            else:
                item.available_at += timedelta(seconds=1)
            session.commit()
        return True

    monkeypatch.setattr(main, "emit_middleware", emit)
    result = asyncio.run(dispatch())
    assert sum(result.values()) == 0
    with database() as session:
        assert session.get(model, "item").state == "PROCESSING"
        assert session.get(model, "item").attempts == 0


def test_status_scopes_backlog_to_tenant_and_preserves_dead_letters(database):
    with database() as session:
        hidden = event("hidden", state="DEAD_LETTER")
        hidden.tenant_id = "b"
        session.add_all([hidden, event("dead", state="DEAD_LETTER"), usage(state="RETRY")])
        session.commit()
        report = status(session, "a")
        assert report["queues"]["events"]["DEAD_LETTER"] == 1
        assert report["queues"]["usage"]["RETRY"] == 1
        assert report["reconciliation_required"] is True
        assert report["delivery_verified"] is False


@pytest.mark.parametrize("limit", [0, 51, -1, True, "50"])
def test_invalid_limit_rejected_before_database_access(limit):
    with pytest.raises(ValueError, match="limit_out_of_range"):
        asyncio.run(dispatch(limit))
