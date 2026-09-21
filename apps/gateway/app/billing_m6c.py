"""Browser billing documents backed by canonical M6C billing facts."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .billing import Credit, CreditNote, Invoice, InvoiceLine, Payment
from .billing_browser import billing_context, decimal, iso
from .billing_documents import (
    DocumentLine,
    credit_note_document,
    invoice_document,
    receipt_document,
    require_confirmed_payment,
)
from .main import db

router = APIRouter(prefix="/app/api/billing", tags=["Browser billing documents"])


def _invoice(s: Session, invoice_id: str, tenant_id: str) -> Invoice:
    item = s.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id))
    if item is None:
        raise HTTPException(404, "invoice_not_found")
    return item


def _document_payload(document: Any) -> dict[str, Any]:
    return {
        "document_type": document.document_type,
        "document_number": document.document_number,
        "tenant_id": document.tenant_id,
        "currency": document.currency,
        "total": decimal(document.total),
        "source_reference": document.source_reference,
        "tax_evidence": document.tax_evidence,
        "lines": [
            {"description": line.description, "quantity": line.quantity, "amount": decimal(line.amount)}
            for line in document.lines
        ],
        "content_hash": document.content_hash,
    }


@router.get("/invoices/{invoice_id}/document")
def invoice_document_get(
    invoice_id: str,
    ctx: dict[str, Any] = Depends(billing_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    invoice = _invoice(s, invoice_id, ctx["tenant"])
    lines = tuple(
        DocumentLine(line.description, line.quantity, line.amount)
        for line in s.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.id)).all()
    )
    return _document_payload(
        invoice_document(
            tenant_id=invoice.tenant_id,
            number=invoice.number,
            currency=invoice.currency,
            lines=lines,
            total=invoice.total,
            tax_evidence=invoice.evidence_json,
        )
    )


@router.get("/credit-notes")
def credit_notes(
    ctx: dict[str, Any] = Depends(billing_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    rows = s.scalars(
        select(CreditNote).where(CreditNote.tenant_id == ctx["tenant"]).order_by(CreditNote.created_at.desc(), CreditNote.id.desc())
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "number": item.number,
                "invoice_id": item.invoice_id,
                "amount": decimal(item.amount),
                "currency": item.currency,
                "reason": item.reason,
                "created_at": iso(item.created_at),
            }
            for item in rows
        ]
    }


@router.get("/credit-notes/{credit_note_id}/document")
def credit_note_document_get(
    credit_note_id: str,
    ctx: dict[str, Any] = Depends(billing_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    item = s.scalar(select(CreditNote).where(CreditNote.id == credit_note_id, CreditNote.tenant_id == ctx["tenant"]))
    if item is None:
        raise HTTPException(404, "credit_note_not_found")
    invoice = _invoice(s, item.invoice_id, ctx["tenant"])
    return _document_payload(
        credit_note_document(
            tenant_id=item.tenant_id,
            number=item.number,
            invoice_number=invoice.number,
            currency=item.currency,
            amount=item.amount,
            reason=item.reason,
        )
    )


@router.get("/payments/{payment_id}/receipt")
def payment_receipt(
    payment_id: str,
    ctx: dict[str, Any] = Depends(billing_context),
    s: Session = Depends(db),
) -> dict[str, Any]:
    payment = s.scalar(select(Payment).where(Payment.id == payment_id, Payment.tenant_id == ctx["tenant"]))
    if payment is None:
        raise HTTPException(404, "payment_not_found")
    require_confirmed_payment(payment.status)
    invoice = _invoice(s, payment.invoice_id, ctx["tenant"])
    return _document_payload(
        receipt_document(
            tenant_id=payment.tenant_id,
            payment_reference=payment.provider_reference,
            currency=payment.currency,
            amount=payment.amount,
            invoice_number=invoice.number,
        )
    )