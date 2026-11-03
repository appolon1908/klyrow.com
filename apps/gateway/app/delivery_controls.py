"""Warm-up, IP-pool, and abuse-control authority for outbound delivery."""

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, Domain, Message, Tenant, audit, auth, db, require

router = APIRouter(prefix="/v1", tags=["Delivery controls"])
now = lambda: datetime.now(UTC)


class IpPool(Base):
    __tablename__ = "ip_pools"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)
    postal_pool_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class IpPoolAssignment(Base):
    __tablename__ = "ip_pool_assignments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    domain: Mapped[str] = mapped_column(String)
    stream: Mapped[str] = mapped_column(String)
    pool_id: Mapped[str] = mapped_column(ForeignKey("ip_pools.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", "stream", name="uq_ip_pool_assignment"),
    )


class WarmupSchedule(Base):
    __tablename__ = "warmup_schedules"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    domain: Mapped[str] = mapped_column(String)
    stream: Mapped[str] = mapped_column(String)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    daily_limits_json: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("tenant_id", "domain", "stream", name="uq_warmup_schedule"),)


class ResourceSuspension(Base):
    __tablename__ = "delivery_resource_suspensions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    resource_type: Mapped[str] = mapped_column(String)
    resource_id: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "resource_type", "resource_id", name="uq_delivery_resource_suspension"
        ),
    )


class AbuseAlert(Base):
    __tablename__ = "abuse_alerts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    metrics_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PoolIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    kind: str = Field(pattern="^(SHARED|DEDICATED|FUTURE_DEDICATED)$")
    postal_pool_ref: str | None = Field(default=None, max_length=200)


class AssignIn(BaseModel):
    domain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]+$")
    stream: str = Field(pattern="^(TRANSACTIONAL|MARKETING|SECURITY|SYSTEM|BULK)$")
    pool_id: str


class WarmupIn(BaseModel):
    domain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]+$")
    stream: str = Field(pattern="^(TRANSACTIONAL|MARKETING|SECURITY|SYSTEM|BULK)$")
    starts_at: datetime
    daily_limits: list[int] = Field(min_length=1, max_length=365)


class SuspendIn(BaseModel):
    tenant_id: str
    resource_type: str = Field(pattern="^(TENANT|DOMAIN|SENDER|STREAM|API_KEY|SMTP_CREDENTIAL)$")
    resource_id: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=5, max_length=500)


class AbuseIn(BaseModel):
    tenant_id: str
    bounce_rate: float = Field(ge=0, le=1)
    complaint_rate: float = Field(ge=0, le=1)
    invalid_rate: float = Field(ge=0, le=1)
    volume_ratio: float = Field(ge=0)


@router.post("/admin/ip-pools", status_code=201)
def create_pool(x: PoolIn, ctx=Depends(require("platform_admin")), s: Session = Depends(db)):
    if s.scalar(select(IpPool).where(IpPool.name == x.name)):
        raise HTTPException(409, "ip_pool_exists")
    item = IpPool(id=str(uuid.uuid4()), name=x.name, kind=x.kind, postal_pool_ref=x.postal_pool_ref)
    s.add(item)
    audit(s, ctx, "ip_pool.created")
    s.commit()
    return item


@router.post("/settings/ip-pool-assignment", status_code=201)
def assign_pool(x: AssignIn, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx["role"] not in {"platform_admin", "tenant_admin", "OWNER", "ADMIN"}:
        raise HTTPException(403, "insufficient_role")
    pool = s.get(IpPool, x.pool_id)
    if not pool or not pool.enabled:
        raise HTTPException(404, "ip_pool_not_found")
    if not s.scalar(
        select(Domain).where(
            Domain.tenant_id == ctx["tenant"], Domain.domain == x.domain, Domain.verified == True
        )
    ):
        raise HTTPException(404, "verified_domain_not_found")
    item = s.scalar(
        select(IpPoolAssignment).where(
            IpPoolAssignment.tenant_id == ctx["tenant"],
            IpPoolAssignment.domain == x.domain,
            IpPoolAssignment.stream == x.stream,
        )
    ) or IpPoolAssignment(
        id=str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        domain=x.domain,
        stream=x.stream,
        pool_id=pool.id,
    )
    item.pool_id = pool.id
    s.add(item)
    audit(s, ctx, "ip_pool.assigned")
    s.commit()
    return {"id": item.id, "pool": pool.name}


@router.post("/settings/warmup", status_code=201)
def warmup(x: WarmupIn, ctx=Depends(auth), s: Session = Depends(db)):
    if ctx["role"] not in {"platform_admin", "tenant_admin", "OWNER", "ADMIN"}:
        raise HTTPException(403, "insufficient_role")
    if not s.scalar(
        select(Domain).where(
            Domain.tenant_id == ctx["tenant"], Domain.domain == x.domain, Domain.verified == True
        )
    ):
        raise HTTPException(404, "verified_domain_not_found")
    if any(value < 1 for value in x.daily_limits) or any(
        b < a for a, b in zip(x.daily_limits, x.daily_limits[1:])
    ):
        raise HTTPException(422, "warmup_limits_must_increase")
    item = s.scalar(
        select(WarmupSchedule).where(
            WarmupSchedule.tenant_id == ctx["tenant"],
            WarmupSchedule.domain == x.domain,
            WarmupSchedule.stream == x.stream,
        )
    ) or WarmupSchedule(
        id=str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        domain=x.domain,
        stream=x.stream,
        starts_at=x.starts_at,
        daily_limits_json="[]",
    )
    item.starts_at = x.starts_at
    item.daily_limits_json = json.dumps(x.daily_limits)
    item.active = True
    s.add(item)
    audit(s, ctx, "warmup.configured")
    s.commit()
    return {"id": item.id, "daily_limits": x.daily_limits}


@router.post("/admin/delivery/suspend", status_code=201)
def suspend_resource(
    x: SuspendIn, ctx=Depends(require("platform_admin")), s: Session = Depends(db)
):
    tenant_id = x.tenant_id
    if not s.get(Tenant, tenant_id):
        raise HTTPException(404, "tenant_not_found")
    if x.resource_type == "TENANT":
        if x.resource_id != tenant_id:
            raise HTTPException(422, "tenant_resource_mismatch")
        s.get(Tenant, tenant_id).enabled = False
    item = s.scalar(
        select(ResourceSuspension).where(
            ResourceSuspension.tenant_id == tenant_id,
            ResourceSuspension.resource_type == x.resource_type,
            ResourceSuspension.resource_id == x.resource_id,
        )
    ) or ResourceSuspension(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        resource_type=x.resource_type,
        resource_id=x.resource_id,
        reason=x.reason,
        created_by=ctx["sub"],
    )
    item.reason = x.reason
    item.active = True
    s.add(item)
    audit(s, {**ctx, "tenant": tenant_id}, "delivery_resource.suspended")
    s.commit()
    return {"id": item.id, "effective_immediately": True}


@router.post("/admin/delivery/suspensions/{item_id}/release")
def release_resource(
    item_id: str, ctx=Depends(require("platform_admin")), s: Session = Depends(db)
):
    item = s.get(ResourceSuspension, item_id)
    if not item or not item.active:
        raise HTTPException(404, "active_suspension_not_found")
    item.active = False
    if item.resource_type == "TENANT":
        s.get(Tenant, item.tenant_id).enabled = True
    audit(s, {**ctx, "tenant": item.tenant_id}, "delivery_resource.released")
    s.commit()
    return {"id": item.id, "active": False, "effective_immediately": True}


@router.post("/admin/abuse/evaluate", status_code=201)
def evaluate_abuse(x: AbuseIn, ctx=Depends(require("platform_admin")), s: Session = Depends(db)):
    if not s.get(Tenant, x.tenant_id):
        raise HTTPException(404, "tenant_not_found")
    triggered = (
        x.complaint_rate >= 0.005
        or x.bounce_rate >= 0.10
        or x.invalid_rate >= 0.10
        or x.volume_ratio >= 5
    )
    severity = "CRITICAL" if x.complaint_rate >= 0.01 or x.bounce_rate >= 0.20 else "HIGH"
    if not triggered:
        return {"state": "GOOD", "suspended": False}
    item = AbuseAlert(
        id=str(uuid.uuid4()),
        tenant_id=x.tenant_id,
        kind="DELIVERY_REPUTATION",
        severity=severity,
        metrics_json=x.model_dump_json(),
    )
    s.add(item)
    if severity == "CRITICAL":
        s.get(Tenant, x.tenant_id).enabled = False
    audit(s, {**ctx, "tenant": x.tenant_id}, "abuse.alert.created")
    s.commit()
    return {
        "id": item.id,
        "state": "SUSPENDED" if severity == "CRITICAL" else "LIMITED",
        "suspended": severity == "CRITICAL",
    }


def enforce_delivery_controls(s: Session, tenant_id: str, sender: str, stream: str):
    domain = sender.rsplit("@", 1)[-1].lower()
    identifiers = {
        "DOMAIN": domain,
        "SENDER": sender.lower(),
        "STREAM": stream.upper(),
        "TENANT": tenant_id,
    }
    for kind, value in identifiers.items():
        if s.scalar(
            select(ResourceSuspension.id).where(
                ResourceSuspension.tenant_id == tenant_id,
                ResourceSuspension.resource_type == kind,
                ResourceSuspension.resource_id == value,
                ResourceSuspension.active == True,
            )
        ):
            raise HTTPException(403, "delivery_resource_suspended")
    schedule = s.scalar(
        select(WarmupSchedule).where(
            WarmupSchedule.tenant_id == tenant_id,
            WarmupSchedule.domain == domain,
            WarmupSchedule.stream == stream.upper(),
            WarmupSchedule.active == True,
        )
    )
    if schedule:
        start = (
            schedule.starts_at
            if schedule.starts_at.tzinfo
            else schedule.starts_at.replace(tzinfo=UTC)
        )
        day = max(0, (now() - start).days)
        limits = json.loads(schedule.daily_limits_json)
        limit = limits[min(day, len(limits) - 1)]
        sent = (
            s.scalar(
                select(func.count(Message.id)).where(
                    Message.tenant_id == tenant_id,
                    Message.sender.like("%@" + domain),
                    Message.created_at >= now().replace(hour=0, minute=0, second=0, microsecond=0),
                )
            )
            or 0
        )
        if sent >= limit:
            raise HTTPException(429, "warmup_daily_limit_reached")
