from datetime import datetime, timezone
import os
from unittest.mock import Mock
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.platform import app
from apps.gateway.app.main import auth, db
from apps.gateway.app.billing import UsageEvent
from apps.gateway.app.usage_history import UsageHistoryQuery, usage_history


def event(tenant, occurred, quantity=1, unit="accepted_message"):
    ident = str(uuid.uuid4())
    return UsageEvent(id=ident, tenant_id=tenant, subscription_id="subscription", message_id=ident,
                      event_key=ident, unit=unit, quantity=quantity, price_id="price",
                      occurred_at=datetime.fromisoformat(occurred.replace("Z", "+00:00")))


@pytest.fixture
def usage_client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    UsageEvent.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([
            event("a", "2026-01-14T23:59:59Z", 99),
            event("a", "2026-01-15T00:00:00Z", 3),
            event("a", "2026-02-01T00:00:00Z", 4),
            event("a", "2026-02-28T23:59:59Z", 5),
            event("a", "2026-03-09T23:59:59Z", 6),
            event("a", "2026-03-10T00:00:00Z", 99),
            event("b", "2026-02-01T00:00:00Z", 1000),
            event("a", "2026-02-01T00:00:00Z", 100, "other_unit"),
        ])
        session.commit()
    def session_dependency():
        with Session(engine) as session:
            yield session
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[db] = session_dependency
    app.dependency_overrides[auth] = lambda: {"tenant": "a", "sub": "reader", "role": "tenant_user"}
    client = TestClient(app)
    try:
        yield client, engine
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        engine.dispose()


def test_monthly_totals_respect_tenant_unit_and_partial_window(usage_client):
    client, _ = usage_client
    response = client.get("/v1/usage/monthly", params={"from": "2026-01-15", "to": "2026-03-10"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["window_start"] == "2026-01-15T00:00:00Z"
    assert data["window_end"] == "2026-03-10T00:00:00Z"
    assert data["items"] == [
        {"period_start": "2026-01-01", "quantity": 3},
        {"period_start": "2026-02-01", "quantity": 9},
        {"period_start": "2026-03-01", "quantity": 6},
    ]
    assert data["unit"] == "accepted_message"
    assert data["next_cursor"] is None


def test_daily_cursor_paginates_aggregates_and_keeps_original_window(usage_client, monkeypatch):
    client, _ = usage_client
    first = client.get("/v1/usage/daily", params={"from": "2026-01-15", "to": "2026-03-10", "limit": 2}).json()
    assert [row["quantity"] for row in first["items"]] == [3, 4]
    monkeypatch.setattr("apps.gateway.app.usage_history.utc_today", lambda: datetime(2027, 1, 1).date())
    second = client.get("/v1/usage/daily", params={"cursor": first["next_cursor"], "limit": 2}).json()
    assert [row["quantity"] for row in second["items"]] == [5, 6]
    assert second["window_start"] == first["window_start"]
    assert second["window_end"] == first["window_end"]
    assert second["next_cursor"] is None


def test_totals_include_every_ledger_row_not_just_first_500(usage_client):
    client, engine = usage_client
    with Session(engine) as session:
        session.add_all(event("a", "2026-04-01T12:00:00Z") for _ in range(601))
        session.commit()
    data = client.get("/v1/usage/daily", params={"from": "2026-04-01", "to": "2026-04-02"}).json()
    assert data["items"] == [{"period_start": "2026-04-01", "quantity": 601}]


def test_custom_unit_is_independent_and_empty_range_is_sparse(usage_client):
    client, _ = usage_client
    data = client.get("/v1/usage/daily", params={"from": "2026-02-01", "to": "2026-02-02", "unit": "other_unit"}).json()
    assert data["items"] == [{"period_start": "2026-02-01", "quantity": 100}]
    empty = client.get("/v1/usage/daily", params={"from": "2026-04-01", "to": "2026-04-02"}).json()
    assert empty["items"] == [] and empty["next_cursor"] is None


@pytest.mark.parametrize("params", [
    {"from": "2026-02-01", "to": "2026-02-01"},
    {"from": "2026-03-01", "to": "2026-02-01"},
    {"from": "2024-01-01", "to": "2026-01-01"},
    {"from": "not-a-date"}, {"to": "0001-01-01"},
    {"cursor": "!!!!"}, {"cursor": "e30="}, {"limit": 0}, {"limit": 101},
    {"project_id": "unimplemented-project"}, {"tenant_id": "b"},
])
def test_invalid_or_unimplemented_filters_are_not_silently_ignored(usage_client, params):
    client, _ = usage_client
    assert client.get("/v1/usage/daily", params=params).status_code == 422


def test_cursor_cannot_change_tenant_unit_granularity_or_date_filter(usage_client):
    client, _ = usage_client
    cursor = client.get("/v1/usage/daily", params={"from": "2026-01-15", "to": "2026-03-10", "limit": 1}).json()["next_cursor"]
    assert client.get("/v1/usage/monthly", params={"cursor": cursor}).status_code == 422
    assert client.get("/v1/usage/daily", params={"cursor": cursor, "unit": "other_unit"}).status_code == 422
    assert client.get("/v1/usage/daily", params={"cursor": cursor, "from": "2026-01-16"}).status_code == 422
    app.dependency_overrides[auth] = lambda: {"tenant": "b", "sub": "other", "role": "tenant_user"}
    assert client.get("/v1/usage/daily", params={"cursor": cursor}).status_code == 422
    data = client.get("/v1/usage/daily", params={"from": "2026-02-01", "to": "2026-02-02"}).json()
    assert data["items"] == [{"period_start": "2026-02-01", "quantity": 1000}]


def test_database_error_is_redacted(usage_client):
    client, _ = usage_client
    session = Mock()
    session.get_bind.return_value.dialect.name = "sqlite"
    session.execute.side_effect = OperationalError("private SQL", {}, Exception("private credential"))
    app.dependency_overrides[db] = lambda: session
    response = client.get("/v1/usage/daily")
    assert response.status_code == 503
    assert response.json()["detail"] == "usage_store_unavailable"
    assert "private" not in response.text


def test_history_requires_authentication():
    client = TestClient(app)
    try:
        assert client.get("/v1/usage/daily").status_code == 401
        assert client.get("/v1/usage/monthly").status_code == 401
    finally:
        client.close()


@pytest.mark.skipif(not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="requires isolated PostgreSQL contract database")
def test_postgres_buckets_use_utc_independent_of_session_timezone():
    engine = create_engine(os.environ["KLYROW_CONTRACT_POSTGRES_URL"])
    UsageEvent.__table__.create(engine, checkfirst=True)
    tenant = str(uuid.uuid4())
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                original_timeout = connection.scalar(text("SHOW statement_timeout"))
                connection.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    session.add(event(tenant, "2026-08-01T00:30:00Z", 7))
                    session.flush()
                    query = UsageHistoryQuery(**{"from": "2026-08-01", "to": "2026-08-02"})
                    for granularity in ("day", "month"):
                        result = usage_history(query, granularity, {"tenant": tenant}, session)
                        assert [item.model_dump(mode="json") for item in result.items] == [{"period_start": "2026-08-01", "quantity": 7}]
                        assert connection.scalar(text("SHOW statement_timeout")) == "5s"
            finally:
                transaction.rollback()
            assert connection.scalar(text("SHOW statement_timeout")) == original_timeout
    finally:
        engine.dispose()
