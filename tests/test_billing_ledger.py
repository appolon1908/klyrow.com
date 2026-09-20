from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.main import Base
from apps.gateway.app.billing import Invoice, Payment
from apps.gateway.app.billing_ledger import invoice_balance, post_settlement


@pytest.fixture
def ledger_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        invoice = Invoice(
            id="invoice", number="INV-LEDGER", tenant_id="tenant", subscription_id="subscription",
            currency="USD", subtotal=Decimal("100.00"), tax=Decimal("0.00"),
            discount=Decimal("0.00"), credits=Decimal("0.00"), total=Decimal("100.00"),
            status="OPEN", due_at=datetime.now(timezone.utc),
        )
        session.add(invoice)
        session.commit()
        yield session
    engine.dispose()


def test_settlements_and_refunds_share_one_invoice_balance(ledger_session):
    first = post_settlement(
        ledger_session, tenant_id="tenant", invoice_id="invoice", provider="SANDBOX",
        provider_reference="payment-one", amount=Decimal("50.00"), currency="USD", confirmed_by="system",
    )
    ledger_session.commit()
    assert first.status == "CONFIRMED"
    balance = invoice_balance(ledger_session, ledger_session.get(Invoice, "invoice"))
    assert balance.remaining_due == Decimal("50.00")
    assert balance.financial_status == "PARTIALLY_PAID"

    second = post_settlement(
        ledger_session, tenant_id="tenant", invoice_id="invoice", provider="SANDBOX",
        provider_reference="payment-two", amount=Decimal("50.00"), currency="USD", confirmed_by="system",
    )
    ledger_session.commit()
    assert second.status == "CONFIRMED"
    assert ledger_session.get(Invoice, "invoice").status == "PAID"

    with pytest.raises(ValueError, match="invoice_not_payable"):
        post_settlement(
            ledger_session, tenant_id="tenant", invoice_id="invoice", provider="SANDBOX",
            provider_reference="payment-three", amount=Decimal("0.01"), currency="USD", confirmed_by="system",
        )


def test_settlement_replay_is_one_payment(ledger_session):
    one = post_settlement(ledger_session, tenant_id="tenant", invoice_id="invoice", provider="SANDBOX", provider_reference="same-provider-reference", amount=Decimal("10.00"), currency="USD", confirmed_by="system")
    two = post_settlement(ledger_session, tenant_id="tenant", invoice_id="invoice", provider="SANDBOX", provider_reference="same-provider-reference", amount=Decimal("10.00"), currency="USD", confirmed_by="system")
    assert one.id == two.id
    assert ledger_session.query(Payment).count() == 1