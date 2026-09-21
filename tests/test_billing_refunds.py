from decimal import Decimal
import uuid

import pytest
from fastapi import HTTPException

from apps.gateway.app.billing_refunds import (
    canonical_refundable_amount,
    confirmed_refund_total,
    refundable_amount,
    validate_dispute_state,
    validate_refund_amount,
    validate_refund_transition,
)
from apps.gateway.app.main import Base, DB, engine


class Refund:
    def __init__(self, amount: str, status: str = "CONFIRMED"):
        self.amount = Decimal(amount)
        self.status = status


def test_refundable_amount_uses_confirmed_refunds_only_from_supplied_records():
    refunds = [Refund("25.00"), Refund("10.00", "PROVIDER_PENDING")]
    assert confirmed_refund_total(refunds) == Decimal("25.00")
    assert refundable_amount("100.00", refunds) == Decimal("75.00")


def test_refund_amount_rejects_over_refund_and_non_positive_amounts():
    with pytest.raises(HTTPException, match="refund_exceeds_refundable_amount"):
        validate_refund_amount("100.00", [Refund("75.00")], "25.01")
    with pytest.raises(HTTPException, match="refund_amount_must_be_positive"):
        validate_refund_amount("100.00", [], "0")


def test_refund_and_dispute_state_transitions_are_fail_closed():
    validate_refund_transition("REQUESTED", "PROVIDER_PENDING")
    validate_dispute_state("EVIDENCE_REQUIRED")
    with pytest.raises(HTTPException, match="invalid_refund_transition"):
        validate_refund_transition("CONFIRMED", "REQUESTED")
    with pytest.raises(HTTPException, match="invalid_dispute_state"):
        validate_dispute_state("PROVIDER_CONFIRMED")


def test_canonical_refundable_amount_uses_confirmed_rows_and_tenant_currency_scope():
    from apps.gateway.app.billing import Credit, Payment, Refund

    suffix = uuid.uuid4().hex
    tenant_id = f"refund-tenant-{suffix}"
    invoice_id = f"refund-invoice-{suffix}"
    payment_id = f"refund-pay-{suffix}"
    Base.metadata.create_all(engine)
    with DB() as session:
        session.add_all([
            Payment(id=payment_id, tenant_id=tenant_id, invoice_id=invoice_id, provider="test", provider_reference=f"refund-provider-a-{suffix}", amount=Decimal("100.00"), currency="USD", status="CONFIRMED"),
            Payment(id=f"refund-pay-pending-{suffix}", tenant_id=tenant_id, invoice_id=invoice_id, provider="test", provider_reference=f"refund-provider-pending-{suffix}", amount=Decimal("50.00"), currency="USD", status="PENDING_RECONCILIATION"),
            Payment(id=f"refund-pay-other-{suffix}", tenant_id="other-tenant", invoice_id=invoice_id, provider="test", provider_reference=f"refund-provider-other-{suffix}", amount=Decimal("40.00"), currency="USD", status="CONFIRMED"),
            Refund(id=f"refund-row-confirmed-{suffix}", tenant_id=tenant_id, payment_id=payment_id, amount=Decimal("25.00"), status="CONFIRMED", provider_reference=f"refund-ref-a-{suffix}"),
            Refund(id=f"refund-row-pending-{suffix}", tenant_id=tenant_id, payment_id=payment_id, amount=Decimal("10.00"), status="PROVIDER_PENDING", provider_reference=f"refund-ref-pending-{suffix}"),
            Credit(id=f"refund-credit-{suffix}", tenant_id=tenant_id, invoice_id=invoice_id, amount=Decimal("5.00"), currency="USD", reason="prior credit"),
        ])
        session.commit()
        assert canonical_refundable_amount(session, tenant_id=tenant_id, invoice_id=invoice_id, currency="USD") == Decimal("70.00")