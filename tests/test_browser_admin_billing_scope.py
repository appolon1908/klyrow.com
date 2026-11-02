"""Platform billing must retain tenant RLS while including every workspace."""
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from apps.gateway.app.main import Audit, Tenant, User, bind_tenant_rls
from apps.gateway.app import browser_admin_ops as admin
from apps.gateway.app.billing import (
    BillingEvent, BillingPlan, BillingPrice, BillingProduct, BillingSubscription,
    BillingWorkItem, Invoice, Payment, Refund,
)
from apps.gateway.app.billing_provider_events import BillingProviderEvent
from apps.gateway.app.payment_attempts import PaymentAttempt, PaymentAttemptEvent


@pytest.fixture(params=["sqlite", "postgresql"])
def billing_database(request, tmp_path):
    postgres = request.param == "postgresql"
    if postgres and not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"):
        pytest.skip("Requires disposable PostgreSQL")
    schema = "admin_billing_" + uuid.uuid4().hex
    owner = None
    if postgres:
        url = os.environ["KLYROW_CONTRACT_POSTGRES_URL"]
        owner = create_engine(url)
        with owner.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'CREATE ROLE "{schema}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        setup = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        setup = create_engine(f"sqlite:///{tmp_path / 'billing.db'}")
    models = (
        Tenant, User, Audit, BillingProduct, BillingPlan, BillingPrice,
        BillingSubscription, Invoice, Payment, Refund, BillingEvent,
        BillingWorkItem, PaymentAttempt, PaymentAttemptEvent, BillingProviderEvent,
    )
    for model in models:
        model.__table__.create(setup)
    now = datetime.now(timezone.utc)
    with Session(setup) as session:
        session.add_all(Tenant(id=tenant, name=tenant, enabled=True, quota=100) for tenant in ("a", "b"))
        session.flush()
        for tenant in ("a", "b"):
            session.add(BillingSubscription(
                id="sub-" + tenant, tenant_id=tenant, plan_id="plan", price_id="price",
                status="ACTIVE", period_end=now + timedelta(days=30),
            ))
            session.add(Invoice(
                id="invoice-" + tenant, number="INV-" + tenant, tenant_id=tenant,
                subscription_id="sub-" + tenant, currency="USD", subtotal=10,
                total=10, status="OPEN", due_at=now - timedelta(days=30),
                created_at=now + timedelta(seconds=tenant == "b"),
            ))
            session.add(Payment(
                id="payment-" + tenant, tenant_id=tenant, invoice_id="invoice-" + tenant,
                provider="SANDBOX", provider_reference="payment-" + tenant,
                amount=1, currency="USD", status="PENDING_RECONCILIATION",
            ))
            session.add(Refund(
                id="refund-" + tenant, tenant_id=tenant, payment_id="payment-" + tenant,
                amount=1, status="PENDING_RECONCILIATION", provider_reference="refund-" + tenant,
            ))
        session.add(User(id="owner", tenant_id="a", email="owner@example.invalid",
                         password_hash="unused", role="platform_admin", enabled=True))
        session.add(User(id="member", tenant_id="a", email="member@example.invalid",
                         password_hash="unused", role="tenant_admin", enabled=True))
        session.add(PaymentAttempt(
            id="attempt-b", tenant_id="b", invoice_id="invoice-b", provider="stripe",
            idempotency_key="attempt-b", request_fingerprint="fixture", amount_minor=1000,
            currency="USD", status="CAPTURED", created_by="owner",
        ))
        session.commit()
    if postgres:
        with setup.begin() as connection:
            connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{schema}"'))
            connection.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO "{schema}"'))
            for model in (Invoice, Payment, Refund, PaymentAttempt, PaymentAttemptEvent, BillingProviderEvent):
                name = model.__tablename__
                connection.execute(text(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY'))
                connection.execute(text(
                    f'CREATE POLICY tenant_isolation ON "{name}" '
                    "USING (tenant_id = current_setting('app.tenant_id', true)) "
                    "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
                ))
        runtime = create_engine(url, connect_args={"options": f"-csearch_path={schema} -crole={schema}"})
    else:
        runtime = setup
    try:
        yield runtime, setup, postgres
    finally:
        runtime.dispose()
        setup.dispose()
        if owner:
            with owner.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                connection.execute(text(f'DROP ROLE "{schema}"'))
            owner.dispose()


def _context(user="owner"):
    return {"sub": user, "tenant": "a", "role": "platform_admin"}


def _assert_request_scope(session, postgres):
    if postgres:
        assert session.scalar(text("SELECT current_setting('app.tenant_id', true)")) == "a"
        assert session.scalars(select(Invoice.id)).all() == ["invoice-a"]


def test_platform_overview_and_reconciliation_include_other_tenant(billing_database):
    runtime, _, postgres = billing_database
    with Session(runtime) as session:
        bind_tenant_rls(session, "a")
        if postgres:
            assert session.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one() == (False, False)
        result = admin.browser_admin_billing_overview(ctx=_context(), session=session)
        assert result["counts"]["invoices"] == {"OPEN": 2}
        assert result["counts"]["subscriptions"] == {"ACTIVE": 2}
        assert result["counts"]["payments"] == {"PENDING_RECONCILIATION": 2}
        assert result["counts"]["refunds"] == {"PENDING_RECONCILIATION": 2}
        assert [row["id"] for row in result["recent_invoices"]] == ["invoice-b", "invoice-a"]
        assert result["billing_drift"] == {"status": "DRIFT", "issue_count": 1}
        report = admin.browser_admin_billing_reconciliation(tenant_id=None, ctx=_context(), session=session)
        assert [(issue["tenant_id"], issue["code"]) for issue in report["issues"]] == [("b", "captured_attempt_missing_payment")]
        filtered = admin.browser_admin_billing_reconciliation(tenant_id="a", ctx=_context(), session=session)
        assert filtered["status"] == "PASS"
        _assert_request_scope(session, postgres)


def test_platform_subscription_invoice_counts_use_each_tenant(billing_database):
    runtime, _, postgres = billing_database
    with Session(runtime) as session:
        bind_tenant_rls(session, "a")
        rows = admin.browser_admin_billing_subscriptions(status=None, tenant_id=None, limit=100, ctx=_context(), session=session)
        assert {row["tenant_id"]: row["open_invoice_count"] for row in rows} == {"a": 1, "b": 1}
        _assert_request_scope(session, postgres)


def test_platform_dunning_updates_both_tenants_without_scope_leak(billing_database, monkeypatch):
    runtime, setup, postgres = billing_database
    monkeypatch.setattr(admin, "load_billing_settings", lambda: SimpleNamespace(enabled=True, dunning_enabled=True))
    with Session(runtime) as session:
        bind_tenant_rls(session, "a")
        result = admin.browser_admin_billing_dunning(admin.AdminDunningIn(), ctx=_context(), _browser_session=None, session=session)
        assert result["processed"] == 2
        assert {item["invoice_id"] for item in result["items"]} == {"invoice-a", "invoice-b"}
        _assert_request_scope(session, postgres)
    with Session(setup) as session:
        assert session.scalars(select(Invoice.status)).all() == ["PAST_DUE", "PAST_DUE"]
        assert session.scalars(select(BillingSubscription.version)).all() == [2, 2]
        assert len(session.scalars(select(BillingEvent)).all()) == 2
    if postgres:
        with runtime.connect() as first, runtime.connect() as second:
            for connection in (first, second):
                assert connection.scalar(text("SELECT current_setting('app.tenant_id', true)")) in (None, "")
                assert connection.execute(select(Invoice)).all() == []


def test_later_tenant_failure_rolls_back_earlier_dunning_commit(billing_database, monkeypatch):
    runtime, setup, postgres = billing_database
    monkeypatch.setattr(admin, "load_billing_settings", lambda: SimpleNamespace(enabled=True, dunning_enabled=True))
    original = admin.run_dunning
    visited = []

    def fail_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        visited.append(kwargs["ctx"]["tenant"])
        if visited[-1] == "b":
            raise RuntimeError("synthetic later-tenant failure")
        return result

    monkeypatch.setattr(admin, "run_dunning", fail_after_commit)
    with Session(runtime) as session:
        bind_tenant_rls(session, "a")
        with pytest.raises(RuntimeError, match="later-tenant failure"):
            admin.browser_admin_billing_dunning(admin.AdminDunningIn(), ctx=_context(), _browser_session=None, session=session)
        assert visited == ["a", "b"]
        _assert_request_scope(session, postgres)
    with Session(setup) as session:
        assert session.scalars(select(Invoice.status)).all() == ["OPEN", "OPEN"]
        assert session.scalars(select(BillingSubscription.version)).all() == [1, 1]
        assert session.scalars(select(BillingEvent)).all() == []
        assert session.scalars(select(Audit)).all() == []
    if postgres:
        with runtime.connect() as first, runtime.connect() as second:
            for connection in (first, second):
                assert connection.scalar(text("SELECT current_setting('app.tenant_id', true)")) in (None, "")
                assert connection.execute(select(Invoice)).all() == []


def test_dunning_never_updates_subscription_from_another_tenant(billing_database, monkeypatch):
    runtime, setup, _ = billing_database
    with Session(setup) as session:
        session.get(Invoice, "invoice-a").subscription_id = "sub-b"
        session.commit()
    monkeypatch.setattr(admin, "load_billing_settings", lambda: SimpleNamespace(enabled=True, dunning_enabled=True))
    with Session(runtime) as session:
        bind_tenant_rls(session, "a")
        admin.browser_admin_billing_dunning(admin.AdminDunningIn(), ctx=_context(), _browser_session=None, session=session)
    with Session(setup) as session:
        assert session.get(BillingSubscription, "sub-a").version == 1
        assert session.get(BillingSubscription, "sub-b").version == 2


@pytest.mark.parametrize("operation", ["overview", "reconciliation", "subscriptions", "dunning"])
def test_untrusted_context_role_cannot_open_platform_scope(billing_database, operation):
    runtime, _, postgres = billing_database
    with Session(runtime) as session:
        bind_tenant_rls(session, "a")
        calls = {
            "overview": lambda: admin.browser_admin_billing_overview(ctx=_context("member"), session=session),
            "reconciliation": lambda: admin.browser_admin_billing_reconciliation(tenant_id=None, ctx=_context("member"), session=session),
            "subscriptions": lambda: admin.browser_admin_billing_subscriptions(status=None, tenant_id=None, limit=100, ctx=_context("member"), session=session),
            "dunning": lambda: admin.browser_admin_billing_dunning(admin.AdminDunningIn(), ctx=_context("member"), _browser_session=None, session=session),
        }
        with pytest.raises(HTTPException) as error:
            calls[operation]()
        assert (error.value.status_code, error.value.detail) == (403, "platform_admin_required")
        _assert_request_scope(session, postgres)
