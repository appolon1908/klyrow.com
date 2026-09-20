import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.main import Base
from apps.gateway.app.billing import Invoice, Payment
from apps.gateway.app.billing_provider_events import BillingProviderEvent
from apps.gateway.app.billing_settlement_worker import process_stripe_provider_event
from apps.gateway.app.payment_attempts import PENDING, PaymentAttempt


def test_claimed_stripe_success_settles_once_and_marks_event_processed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        invoice = Invoice(id="invoice", number="INV-WORKER", tenant_id="tenant", subscription_id="sub", currency="USD", subtotal=Decimal("10.00"), tax=Decimal("0"), discount=Decimal("0"), credits=Decimal("0"), total=Decimal("10.00"), status="OPEN", due_at=datetime.now(timezone.utc))
        attempt = PaymentAttempt(id="attempt", tenant_id="tenant", invoice_id="invoice", provider="stripe", idempotency_key="key", request_fingerprint="fingerprint", amount_minor=1000, currency="USD", status=PENDING, created_by="test")
        payload = {"id": "evt_1", "type": "payment_intent.succeeded", "livemode": False, "data": {"object": {"id": "pi_1", "amount_received": 1000, "currency": "usd", "metadata": {"tenant_id": "tenant", "invoice_id": "invoice", "payment_attempt_id": "attempt"}}}}
        event = BillingProviderEvent(id="event", provider="stripe", provider_event_id="evt_1", event_type="payment_intent.succeeded", tenant_id="tenant", invoice_id="invoice", payment_attempt_id="attempt", livemode=False, payload_json=json.dumps(payload), payload_hash="hash", processing_state="PROCESSING")
        session.add_all([invoice, attempt, event]); session.flush()
        process_stripe_provider_event(session, event)
        session.commit()
        assert event.processing_state == "PROCESSED"
        assert attempt.status == "CAPTURED"
        assert session.query(Payment).filter_by(payment_attempt_id="attempt").count() == 1
        assert session.get(Invoice, "invoice").status == "PAID"
        process_stripe_provider_event(session, event)
        assert session.query(Payment).filter_by(payment_attempt_id="attempt").count() == 1
    engine.dispose()


def test_checkout_completed_without_paid_status_does_not_settle():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        invoice = Invoice(id="invoice-unpaid", number="INV-UNPAID", tenant_id="tenant", subscription_id="sub", currency="USD", subtotal=Decimal("10.00"), tax=Decimal("0"), discount=Decimal("0"), credits=Decimal("0"), total=Decimal("10.00"), status="OPEN", due_at=datetime.now(timezone.utc))
        attempt = PaymentAttempt(id="attempt-unpaid", tenant_id="tenant", invoice_id="invoice-unpaid", provider="stripe", idempotency_key="key-unpaid", request_fingerprint="fingerprint", amount_minor=1000, currency="USD", status=PENDING, created_by="test")
        payload = {"id": "evt_unpaid", "type": "checkout.session.completed", "livemode": False, "data": {"object": {"id": "cs_1", "payment_status": "unpaid", "amount_total": 1000, "currency": "usd", "payment_intent": "pi_1", "metadata": {"tenant_id": "tenant", "invoice_id": "invoice-unpaid", "payment_attempt_id": "attempt-unpaid"}}}}
        event = BillingProviderEvent(id="event-unpaid", provider="stripe", provider_event_id="evt_unpaid", event_type="checkout.session.completed", tenant_id="tenant", invoice_id="invoice-unpaid", payment_attempt_id="attempt-unpaid", livemode=False, payload_json=json.dumps(payload), payload_hash="hash", processing_state="PROCESSING")
        session.add_all([invoice, attempt, event]); session.flush()
        process_stripe_provider_event(session, event); session.commit()
        assert event.processing_state == "PROCESSED" and event.last_error_code == "stripe_checkout_not_paid"
        assert attempt.status == PENDING
        assert session.query(Payment).filter_by(payment_attempt_id="attempt-unpaid").count() == 0
    engine.dispose()