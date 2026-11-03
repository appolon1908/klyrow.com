"""Tenant-isolated reseller and subaccount authority."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, Tenant, audit, auth, db, require

router = APIRouter(prefix="/v1", tags=["Resellers"])


class Reseller(Base):
    __tablename__ = "resellers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String)
    wholesale_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ResellerCustomer(Base):
    __tablename__ = "reseller_customers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    reseller_id: Mapped[str] = mapped_column(ForeignKey("resellers.id"), index=True)
    customer_tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), unique=True, index=True
    )
    retail_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    quota: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        UniqueConstraint("reseller_id", "customer_tenant_id", name="uq_reseller_customer"),
    )


class ResellerIn(BaseModel):
    tenant_id: str
    name: str = Field(min_length=1, max_length=200)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    wholesale_rate: Decimal = Field(ge=0)
    credit_limit: Decimal = Field(ge=0)


class SubaccountIn(BaseModel):
    customer_tenant_id: str
    retail_rate: Decimal = Field(ge=0)
    quota: int = Field(gt=0)


def owned_reseller(s: Session, ctx):
    item = s.scalar(
        select(Reseller).where(Reseller.tenant_id == ctx["tenant"], Reseller.active == True)
    )
    if not item:
        raise HTTPException(404, "reseller_not_found")
    if ctx["role"] not in {"reseller_admin", "tenant_admin", "platform_admin"}:
        raise HTTPException(403, "insufficient_role")
    return item


@router.post("/admin/resellers", status_code=201)
def create_reseller(
    x: ResellerIn, ctx=Depends(require("platform_admin")), s: Session = Depends(db)
):
    if not s.get(Tenant, x.tenant_id):
        raise HTTPException(404, "tenant_not_found")
    if s.scalar(select(Reseller).where(Reseller.tenant_id == x.tenant_id)):
        raise HTTPException(409, "tenant_already_reseller")
    item = Reseller(
        id=str(uuid.uuid4()),
        tenant_id=x.tenant_id,
        name=x.name,
        currency=x.currency,
        wholesale_rate=x.wholesale_rate,
        credit_limit=x.credit_limit,
    )
    s.add(item)
    audit(s, ctx, "reseller.created")
    s.commit()
    return {"id": item.id, "tenant_id": item.tenant_id, "name": item.name}


@router.post("/reseller/subaccounts", status_code=201)
def add_subaccount(x: SubaccountIn, ctx=Depends(auth), s: Session = Depends(db)):
    reseller = owned_reseller(s, ctx)
    customer = s.get(Tenant, x.customer_tenant_id)
    if not customer:
        raise HTTPException(404, "customer_tenant_not_found")
    if customer.id == reseller.tenant_id:
        raise HTTPException(409, "reseller_cannot_be_own_customer")
    if s.scalar(select(ResellerCustomer).where(ResellerCustomer.customer_tenant_id == customer.id)):
        raise HTTPException(409, "customer_already_assigned")
    item = ResellerCustomer(
        id=str(uuid.uuid4()),
        reseller_id=reseller.id,
        customer_tenant_id=customer.id,
        retail_rate=x.retail_rate,
        quota=x.quota,
    )
    s.add(item)
    audit(s, ctx, "reseller.subaccount.created")
    s.commit()
    return {"id": item.id, "customer_tenant_id": item.customer_tenant_id, "quota": item.quota}


@router.get("/reseller/subaccounts")
def list_subaccounts(ctx=Depends(auth), s: Session = Depends(db)):
    reseller = owned_reseller(s, ctx)
    return s.scalars(
        select(ResellerCustomer).where(
            ResellerCustomer.reseller_id == reseller.id, ResellerCustomer.active == True
        )
    ).all()


@router.delete("/reseller/subaccounts/{item_id}")
def remove_subaccount(item_id: str, ctx=Depends(auth), s: Session = Depends(db)):
    reseller = owned_reseller(s, ctx)
    item = s.scalar(
        select(ResellerCustomer).where(
            ResellerCustomer.id == item_id, ResellerCustomer.reseller_id == reseller.id
        )
    )
    if not item:
        raise HTTPException(404, "not_found")
    item.active = False
    audit(s, ctx, "reseller.subaccount.disabled")
    s.commit()
    return {"id": item.id, "active": False}
