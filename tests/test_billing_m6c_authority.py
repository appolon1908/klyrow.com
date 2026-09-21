from decimal import Decimal

import pytest
from fastapi import HTTPException

from apps.gateway.app.billing_disputes import (
    DisputeEvidence,
    require_tenant_evidence,
    validate_dispute_transition,
)
from apps.gateway.app.billing_documents import (
    DocumentLine,
    credit_note_document,
    invoice_document,
    receipt_document,
    require_confirmed_payment,
)
from apps.gateway.app.billing_tax import TaxSnapshot, calculate_tax, invoice_tax_evidence


def test_dispute_transitions_are_explicit():
    validate_dispute_transition("OPEN", "EVIDENCE_REQUIRED")
    with pytest.raises(HTTPException, match="invalid_dispute_transition"):
        validate_dispute_transition("OPEN", "WON")


def test_dispute_evidence_is_tenant_scoped():
    evidence = DisputeEvidence("e1", "tenant-a", "d1", "invoice", "hash", __import__("datetime").datetime.now())
    assert require_tenant_evidence([evidence], "tenant-a") == (evidence,)
    with pytest.raises(HTTPException, match="dispute_not_found"):
        require_tenant_evidence([evidence], "tenant-b")


def test_tax_snapshot_is_deterministic_and_immutable():
    snapshot = TaxSnapshot("GB", "VAT", Decimal("0.20"), "customer_location")
    assert calculate_tax("10.00", snapshot) == Decimal("2.00")
    evidence = invoice_tax_evidence(snapshot, "10.00")
    assert evidence["tax"] == "2.00"
    assert evidence["snapshot"] == snapshot.as_json()


def test_documents_are_reproducible_from_canonical_facts():
    lines = (DocumentLine("Subscription", 1, Decimal("10.00")),)
    first = invoice_document(tenant_id="tenant-a", number="INV-1", currency="GBP", lines=lines, total=Decimal("12.00"))
    second = invoice_document(tenant_id="tenant-a", number="INV-1", currency="GBP", lines=lines, total=Decimal("12.00"))
    assert first.content_hash == second.content_hash
    assert credit_note_document(tenant_id="tenant-a", number="CN-1", invoice_number="INV-1", currency="GBP", amount=Decimal("2.00"), reason="Adjustment").document_type == "CREDIT_NOTE"


def test_receipts_require_confirmed_payment():
    require_confirmed_payment("CONFIRMED")
    with pytest.raises(HTTPException, match="receipt_requires_confirmed_payment"):
        require_confirmed_payment("PENDING_RECONCILIATION")
    assert receipt_document(tenant_id="tenant-a", payment_reference="pay-1", currency="GBP", amount=Decimal("10.00"), invoice_number="INV-1").document_type == "RECEIPT"
