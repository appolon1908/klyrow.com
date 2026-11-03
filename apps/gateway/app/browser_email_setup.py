"""Same-origin browser façade for domain and sender setup.

This deliberately reuses the public messaging domain/sender primitives so the
browser dashboard and API clients share one authorization and validation path.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .main import db
from .auth_bff import BrowserSession, browser_context, csrf_guard
from .messaging import (
    DomainClaimIn,
    SenderIn,
    domain_claim,
    domain_claims,
    domain_verify,
    sender_create,
    senders,
)

router = APIRouter(tags=["Browser email setup"])


def _management_required(ctx: dict) -> None:
    if ctx.get("role") not in {"OWNER", "ADMIN"}:
        from fastapi import HTTPException

        raise HTTPException(403, "tenant_management_denied")


@router.get("/app/api/domains")
def browser_domains(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    return domain_claims(ctx=ctx, s=s)


@router.post("/app/api/domains", status_code=201)
def browser_domain_create(
    payload: DomainClaimIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    _management_required(ctx)
    return domain_claim(payload, ctx=ctx, s=s)


@router.post("/app/api/domains/{item_id}/verify")
def browser_domain_verify(
    item_id: str,
    payload: dict,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    _management_required(ctx)
    return domain_verify(item_id, payload, ctx=ctx, s=s)


@router.get("/app/api/senders")
def browser_senders(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    return senders(ctx=ctx, s=s)


@router.post("/app/api/senders", status_code=201)
def browser_sender_create(
    payload: SenderIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    _management_required(ctx)
    return sender_create(payload, ctx=ctx, s=s)
