from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.gateway.app.main import Base, Tenant
from apps.gateway.app.billing import Invoice, Payment
from apps.gateway.app.billing_reconciliation import reconcile_billing
from apps.gateway.app.billing_reconciliation import router as reconciliation_router
from apps.gateway.app.billing_reconciliation_worker import run_billing_reconciliation
from apps.gateway.app.billing_provider_events import BillingProviderEvent
from apps.gateway.app.payment_attempts import CAPTURED, PaymentAttempt


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def _invoice(tenant: str, *, status: str = "OPEN") -> Invoice:
    return Invoice(
        id=str(uuid.uuid4()), number="INV-" + uuid.uuid4().hex[:8], tenant_id=tenant,
        subscription_id="sub", currency="USD", subtotal=Decimal("10.00"),
        tax=Decimal("0.00"), discount=Decimal("0.00"), credits=Decimal("0.00"),
        total=Decimal("10.00"), status=status,
        due_at=datetime.now(timezone.utc) + timedelta(days=30),
    )


def test_captured_attempt_without_payment_is_reported_without_mutation():
    engine, session = _session()
    session.add(Tenant(id="tenant-a", name="A", quota=100))
    invoice = _invoice("tenant-a")
    attempt = PaymentAttempt(
        id="attempt-a", tenant_id="tenant-a", invoice_id=invoice.id, provider="stripe",
        idempotency_key="attempt-key", request_fingerprint="fingerprint", amount_minor=1000,
        currency="USD", status=CAPTURED, created_by="user-a",
    )
    session.add_all([invoice, attempt])
    session.commit()

    issues = reconcile_billing(session, tenant_id="tenant-a")

    assert [item.code for item in issues] == ["captured_attempt_missing_payment"]
    assert session.get(PaymentAttempt, "attempt-a").status == CAPTURED
    session.close()
    engine.dispose()


def test_confirmed_payment_and_attempt_mismatches_are_reported():
    engine, session = _session()
    session.add(Tenant(id="tenant-a", name="A", quota=100))
    invoice = _invoice("tenant-a")
    attempt = PaymentAttempt(
        id="attempt-a", tenant_id="tenant-a", invoice_id=invoice.id, provider="stripe",
        idempotency_key="attempt-key", request_fingerprint="fingerprint", amount_minor=1000,
        currency="USD", status="PENDING", created_by="user-a",
    )
    payment = Payment(
        id="payment-a", tenant_id="tenant-a", invoice_id=invoice.id,
        payment_attempt_id=attempt.id, provider="stripe", provider_reference="pi-a",
        amount=Decimal("9.00"), currency="EUR", status="CONFIRMED",
    )
    session.add_all([invoice, attempt, payment])
    session.commit()

    codes = {item.code for item in reconcile_billing(session, tenant_id="tenant-a")}

    assert {"confirmed_payment_attempt_not_captured", "payment_attempt_currency_mismatch", "payment_attempt_amount_mismatch"} <= codes
    session.close()
    engine.dispose()


def test_worker_report_is_read_only_and_stable():
    engine, session = _session()
    session.add(Tenant(id="tenant-a", name="A", quota=100))
    invoice = _invoice("tenant-a", status="PAID")
    session.add(invoice)
    session.commit()

    report = run_billing_reconciliation(session, tenant_id="tenant-a")

    assert report["status"] == "DRIFT"
    assert report["issue_count"] == 1
    assert report["issues"][0]["code"] == "invoice_paid_with_remaining_due"
    assert session.get(Invoice, invoice.id).status == "PAID"
    session.close()
    engine.dispose()


def test_provider_event_exact_correlation_is_reported():
    engine, session = _session()
    session.add(Tenant(id="tenant-a", name="A", quota=100))
    invoice = _invoice("tenant-a")
    attempt = PaymentAttempt(
        id="attempt-a", tenant_id="tenant-a", invoice_id=invoice.id, provider="stripe",
        idempotency_key="attempt-key", request_fingerprint="fingerprint", amount_minor=1000,
        currency="USD", status="PENDING", created_by="user-a",
    )
    event = BillingProviderEvent(
        id="event-a", provider="stripe", provider_event_id="evt-a",
        event_type="payment_intent.succeeded", tenant_id="tenant-a",
        payment_attempt_id=attempt.id, invoice_id="wrong-invoice", livemode=False,
        payload_json='{"data":{"object":{"currency":"EUR","amount_received":900}}}',
        payload_hash="hash", processing_state="RECEIVED",
    )
    session.add_all([invoice, attempt, event])
    session.commit()

    codes = {item.code for item in reconcile_billing(session, tenant_id="tenant-a")}

    assert {"provider_event_invoice_mismatch", "provider_event_currency_mismatch", "provider_event_amount_mismatch"} <= codes
    session.close()
    engine.dispose()


def test_billing_rls_migration_covers_financial_tables_and_runtime_role():
    migration = open(
        "migrations/2026092101_billing_rls_runtime_roles.sql",
        encoding="utf-8",
    ).read()
    assert "klyrow_runtime" in migration
    assert "NOBYPASSRLS" in migration
    for table in (
        "klyrow_invoices", "klyrow_payments", "klyrow_refunds",
        "klyrow_credits", "klyrow_payment_attempts",
        "klyrow_payment_attempt_events", "klyrow_billing_provider_events",
    ):
        assert table in migration
    assert "current_setting(''app.tenant_id'', true)" in migration


def test_reconciliation_api_is_operator_only_and_tenant_filterable():
    route = next(item for item in reconciliation_router.routes if item.path.endswith("/reconciliation"))
    dependency_names = {dependency.call.__name__ for dependency in route.dependant.dependencies}
    assert "inner" in dependency_names
    assert route.methods == {"GET"}
