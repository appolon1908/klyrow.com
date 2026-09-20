from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.gateway.app.main import Base
from apps.gateway.app.billing import Invoice
from apps.gateway.app.billing_checkout import create_or_resume_stripe_checkout
from apps.gateway.app.payment_attempts import PaymentAttempt


def test_checkout_reuses_same_idempotency_attempt_and_rejects_second_active_attempt(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("KLYROW_BILLING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_SECRET_FILE", str(__import__("pathlib").Path("stripe-checkout-secret")))
    monkeypatch.setenv("KLYROW_STRIPE_WEBHOOK_SECRET_FILE", str(__import__("pathlib").Path("stripe-webhook-secret")))
    __import__("pathlib").Path("stripe-checkout-secret").write_text("sk_test_fixture")
    __import__("pathlib").Path("stripe-webhook-secret").write_text("whsec_test_fixture")
    try:
        with Session(engine) as session:
            session.add(Invoice(id="invoice", number="INV-CHECKOUT", tenant_id="tenant", subscription_id="sub", currency="USD", subtotal=Decimal("10"), tax=Decimal("0"), discount=Decimal("0"), credits=Decimal("0"), total=Decimal("10"), status="OPEN", due_at=datetime.now(timezone.utc) + timedelta(days=1)))
            session.commit()
            # Provider construction is intentionally patched: this test covers orchestration, not network I/O.
            class Provider:
                def create_checkout(self, **kwargs):
                    return type("Result", (), {"session_id": "cs_test", "checkout_url": "https://checkout.stripe.test/cs_test"})()
            monkeypatch.setattr("apps.gateway.app.billing_checkout.get_checkout_provider", lambda: Provider())
            first = create_or_resume_stripe_checkout(session, tenant_id="tenant", invoice_id="invoice", actor="user", idempotency_key="checkout-key")
            assert first["status"] == "REQUIRES_ACTION"
            replay = create_or_resume_stripe_checkout(session, tenant_id="tenant", invoice_id="invoice", actor="user", idempotency_key="checkout-key")
            assert replay["duplicate"] is True
            with pytest.raises(Exception) as conflict:
                create_or_resume_stripe_checkout(session, tenant_id="tenant", invoice_id="invoice", actor="user", idempotency_key="other-key")
            assert getattr(conflict.value, "detail", None) == "checkout_already_in_progress"
            assert session.query(PaymentAttempt).count() == 1
    finally:
        __import__("pathlib").Path("stripe-checkout-secret").unlink(missing_ok=True)
        __import__("pathlib").Path("stripe-webhook-secret").unlink(missing_ok=True)
        engine.dispose()