import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.main import Base
from apps.gateway.app.billing import Dispute, Invoice, Payment, Refund
from apps.gateway.app.billing_provider_events import BillingProviderEvent
from apps.gateway.app.billing_reconciliation import reconcile_billing
from apps.gateway.app.billing_settlement_worker import process_paypal_provider_event
from apps.gateway.app.payment_attempts import AUTHORIZED, CAPTURED, FAILED, REQUIRES_ACTION, PaymentAttempt


class FakeCapture:
    def __init__(self):
        self.calls = []

    def capture_order(self, *, order_id, idempotency_key):
        self.calls.append((order_id, idempotency_key))
        return type("Result", (), {"status": "COMPLETED", "capture_id": "CAP-1"})()


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def _invoice_attempt(session):
    invoice = Invoice(
        id="invoice", number="INV-1", tenant_id="tenant", subscription_id="sub",
        currency="USD", subtotal=Decimal("10.00"), tax=Decimal("0.00"),
        discount=Decimal("0.00"), credits=Decimal("0.00"), total=Decimal("10.00"),
        status="OPEN", due_at=datetime.now(timezone.utc),
    )
    attempt = PaymentAttempt(
        id="attempt", tenant_id="tenant", invoice_id="invoice", provider="paypal",
        provider_attempt_reference="ORDER-1", idempotency_key="key",
        request_fingerprint="fingerprint", amount_minor=1000, currency="USD",
        status=REQUIRES_ACTION, created_by="test",
    )
    session.add_all([invoice, attempt])
    session.flush()
    return invoice, attempt


def _event(kind, resource, event_id):
    payload = {"id": event_id, "event_type": kind, "resource": resource}
    return BillingProviderEvent(
        id="row-" + event_id, provider="paypal", provider_event_id=event_id,
        event_type=kind, tenant_id="tenant", invoice_id="invoice", payment_attempt_id="attempt",
        livemode=False, payload_json=json.dumps(payload), payload_hash="hash",
        processing_state="PROCESSING",
    )


def test_paypal_order_approved_requests_capture_without_settlement():
    engine, session = _session()
    _invoice_attempt(session)
    event = _event("CHECKOUT.ORDER.APPROVED", {"id": "ORDER-1"}, "WH-APPROVED")
    session.add(event); session.flush()
    adapter = FakeCapture()

    process_paypal_provider_event(session, event, capture_adapter=adapter)
    session.commit()

    attempt = session.get(PaymentAttempt, "attempt")
    assert attempt.status == AUTHORIZED
    assert adapter.calls == [("ORDER-1", "klyrow-paypal-capture:attempt")]
    assert session.query(Payment).count() == 0
    assert event.processing_state == "PROCESSED"
    session.close(); engine.dispose()


def test_paypal_capture_completed_posts_canonical_payment_once():
    engine, session = _session()
    _invoice_attempt(session)
    approved = _event("CHECKOUT.ORDER.APPROVED", {"id": "ORDER-1"}, "WH-APPROVED")
    session.add(approved); session.flush()
    process_paypal_provider_event(session, approved, capture_adapter=FakeCapture())
    session.commit()

    resource = {
        "id": "CAPTURE-1",
        "amount": {"currency_code": "USD", "value": "10.00"},
        "supplementary_data": {"related_ids": {"order_id": "ORDER-1"}},
    }
    completed = _event("PAYMENT.CAPTURE.COMPLETED", resource, "WH-CAPTURE")
    session.add(completed); session.flush()
    process_paypal_provider_event(session, completed)
    session.commit()

    attempt = session.get(PaymentAttempt, "attempt")
    assert attempt.status == CAPTURED
    payment = session.query(Payment).one()
    assert payment.provider == "paypal"
    assert payment.provider_reference == "CAPTURE-1"
    assert payment.amount == Decimal("10.00")
    assert session.get(Invoice, "invoice").status == "PAID"

    replay = _event("PAYMENT.CAPTURE.COMPLETED", resource, "WH-CAPTURE-REPLAY")
    session.add(replay); session.flush()
    process_paypal_provider_event(session, replay)
    session.commit()
    assert session.query(Payment).count() == 1
    session.close(); engine.dispose()


def test_paypal_capture_denied_fails_active_attempt():
    engine, session = _session()
    _invoice_attempt(session)
    denied = _event(
        "PAYMENT.CAPTURE.DENIED",
        {"id": "CAP-DENIED", "supplementary_data": {"related_ids": {"order_id": "ORDER-1"}}},
        "WH-DENIED",
    )
    session.add(denied); session.flush()
    process_paypal_provider_event(session, denied)
    session.commit()
    assert session.get(PaymentAttempt, "attempt").status == FAILED
    assert session.query(Payment).count() == 0
    session.close(); engine.dispose()


def test_paypal_refund_and_dispute_evidence_are_reconciliation_findings_until_canonical_rows_exist():
    engine, session = _session()
    _invoice_attempt(session)
    refund_event = _event(
        "PAYMENT.CAPTURE.REFUNDED",
        {"id": "RF-1", "supplementary_data": {"related_ids": {"order_id": "ORDER-1"}}},
        "WH-REFUND",
    )
    dispute_event = _event(
        "CUSTOMER.DISPUTE.CREATED",
        {"id": "PP-D-1", "supplementary_data": {"related_ids": {"order_id": "ORDER-1"}}},
        "WH-DISPUTE",
    )
    refund_event.processing_state = dispute_event.processing_state = "PROCESSED"
    session.add_all([refund_event, dispute_event]); session.commit()

    codes = {issue.code for issue in reconcile_billing(session, tenant_id="tenant")}
    assert "paypal_refund_evidence_unmatched" in codes
    assert "paypal_dispute_evidence_unmatched" in codes

    session.add(Refund(
        id="refund", tenant_id="tenant", payment_id="not-yet-known",
        amount=Decimal("1.00"), status="CONFIRMED", provider_reference="RF-1",
    ))
    session.add(Dispute(
        id="dispute", tenant_id="tenant", invoice_id="invoice", payment_id=None,
        provider_reference="PP-D-1", category="PAYMENT", amount=Decimal("1.00"),
        currency="USD", reason="provider evidence", status="OPEN", evidence_json="{}",
    ))
    session.commit()
    codes = {issue.code for issue in reconcile_billing(session, tenant_id="tenant")}
    assert "paypal_refund_evidence_unmatched" not in codes
    assert "paypal_dispute_evidence_unmatched" not in codes
    session.close(); engine.dispose()
