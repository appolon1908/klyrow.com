"""Canonical tenant-local account views backed by existing membership tables."""
import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import auth, db
from .tenancy import Organization, TenantMember

router = APIRouter(tags=["Account"])


class OrganizationView(BaseModel):
    id: str
    name: str
    slug: str
    status: str


class MemberView(BaseModel):
    id: str
    user_id: str
    role: str


class MemberPage(BaseModel):
    items: list[MemberView]
    next_cursor: str | None


@router.get("/v1/organization", response_model=OrganizationView,
            responses={401: {"description": "Authentication required"}, 404: {"description": "Organization not found"}})
def organization(ctx=Depends(auth), s: Session=Depends(db)):
    item = s.scalar(select(Organization).where(Organization.tenant_id == ctx["tenant"]))
    if item is None:
        raise HTTPException(404, "organization_not_found")
    return OrganizationView(id=item.id, name=item.name, slug=item.slug, status=item.status)


@router.get("/v1/members", response_model=MemberPage,
            responses={401: {"description": "Authentication required"}, 422: {"description": "Invalid cursor or limit"}})
def members(limit: int=Query(50, ge=1, le=100), cursor: str | None=Query(None, max_length=2048),
            ctx=Depends(auth), s: Session=Depends(db)):
    query = select(TenantMember).where(TenantMember.tenant_id == ctx["tenant"], TenantMember.active == True)
    if cursor:
        try:
            import json
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if set(value) != {"tenant_id", "after"} or value["tenant_id"] != ctx["tenant"] or not isinstance(value["after"], str) or not 1 <= len(value["after"]) <= 200:
                raise ValueError("invalid cursor")
        except (ValueError, TypeError, binascii.Error) as exc:
            raise HTTPException(422, "invalid_cursor") from exc
        query = query.where(TenantMember.id > value["after"])
    rows = list(s.scalars(query.order_by(TenantMember.id).limit(limit+1)))
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        import json
        next_cursor = base64.urlsafe_b64encode(json.dumps({"tenant_id": ctx["tenant"], "after": page[-1].id}).encode()).decode()
    return MemberPage(items=[MemberView(id=m.id, user_id=m.user_id, role=m.role) for m in page], next_cursor=next_cursor)
