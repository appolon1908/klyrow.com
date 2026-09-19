"""Canonical Klyrow business events and their dedicated transactional outbox.

The table in this module is deliberately separate from the generic integration
outbox.  Its state machine and acceptance contract belong to the Klyrow to
Middleware boundary; no Odoo dependency is present here.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base
from .telemetry import trace_carrier


SOURCES = ("klyrow", "telnexa", "vicidial", "integration")
KLYROW_EVENTS = (
    "klyrow.tenant.created",
    "klyrow.tenant.updated",
    "klyrow.subscription.changed",
    "klyrow.usage.daily",
    "klyrow.kpi.daily",
    "klyrow.campaign.summary",
    "klyrow.domain.status",
    "klyrow.provider.health",
    "klyrow.account.held",
    "klyrow.account.released",
)
KlyrowEventType = Literal[
    "klyrow.tenant.created",
    "klyrow.tenant.updated",
    "klyrow.subscription.changed",
    "klyrow.usage.daily",
    "klyrow.kpi.daily",
    "klyrow.campaign.summary",
    "klyrow.domain.status",
    "klyrow.provider.health",
    "klyrow.account.held",
    "klyrow.account.released",
]
OUTBOX_STATES = ("PENDING", "LEASED", "RETRYING", "DELIVERED", "DEAD_LETTER")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantEventData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=200)
    name: Optional[str] = Field(default=None, max_length=200)
    organization_id: Optional[str] = Field(default=None, max_length=200)
    enabled: bool


class SubscriptionChangedData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription_id: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=50)
    plan_id: Optional[str] = Field(default=None, max_length=200)
    price_id: Optional[str] = Field(default=None, max_length=200)
    version: int = Field(ge=1)
    effective_at: AwareDatetime


class DailyUsageData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    unit: Literal["accepted_message"]
    quantity: int = Field(ge=0)
    snapshot_at: AwareDatetime


class DailyKpiData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    accepted: int = Field(ge=0)
    delivered: int = Field(ge=0)
    bounced: int = Field(ge=0)
    complained: int = Field(ge=0)
    snapshot_at: AwareDatetime


class CampaignSummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1, max_length=200)
    campaign_version: int = Field(ge=1)
    status: Literal["COMPLETED", "CANCELLED", "FAILED"]
    audience_count: int = Field(ge=0)
    delivered_count: int = Field(ge=0)
    suppressed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)


class DomainStatusData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain_id: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=253)
    status: str = Field(min_length=1, max_length=50)
    verified_at: Optional[AwareDatetime] = None


class ProviderHealthData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=100)
    status: Literal["HEALTHY", "DEGRADED", "UNAVAILABLE"]
    checked_at: AwareDatetime
    reason_code: Optional[str] = Field(default=None, max_length=100)


class AccountStateData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["HELD", "RELEASED"]
    reason: str = Field(min_length=1, max_length=300)
    changed_at: AwareDatetime


KLYROW_EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    "klyrow.tenant.created": TenantEventData,
    "klyrow.tenant.updated": TenantEventData,
    "klyrow.subscription.changed": SubscriptionChangedData,
    "klyrow.usage.daily": DailyUsageData,
    "klyrow.kpi.daily": DailyKpiData,
    "klyrow.campaign.summary": CampaignSummaryData,
    "klyrow.domain.status": DomainStatusData,
    "klyrow.provider.health": ProviderHealthData,
    "klyrow.account.held": AccountStateData,
    "klyrow.account.released": AccountStateData,
}


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^evt_[A-Za-z0-9_-]+$", min_length=5, max_length=200)
    type: KlyrowEventType
    version: Literal[1]
    source: Literal["klyrow"]
    tenant_id: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=200)
    causation_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    occurred_at: AwareDatetime
    data: dict

    @field_validator("occurred_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_event_data(self):
        model = KLYROW_EVENT_DATA_MODELS.get(self.type)
        if model is None:
            raise ValueError("unsupported_klyrow_business_event")
        self.data = model.model_validate(self.data).model_dump(mode="json")
        if self.type.startswith("klyrow.tenant.") and self.data["tenant_id"] != self.tenant_id:
            raise ValueError("event_tenant_mismatch")
        if self.type == "klyrow.account.held" and self.data["status"] != "HELD":
            raise ValueError("account_event_status_mismatch")
        if self.type == "klyrow.account.released" and self.data["status"] != "RELEASED":
            raise ValueError("account_event_status_mismatch")
        return self


class BusinessEventOutbox(Base):
    __tablename__ = "business_event_outbox"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    aggregate_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    trace_context: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", name="uq_business_event_tenant_event"),
        CheckConstraint(
            "state IN ('PENDING','LEASED','RETRYING','DELIVERED','DEAD_LETTER')",
            name="ck_business_event_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_business_event_attempts"),
    )


def new_event_id() -> str:
    return "evt_" + uuid.uuid4().hex


def canonical_event(event: EventEnvelope) -> str:
    return json.dumps(event.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)


def enqueue_event(session, event: EventEnvelope, *, aggregate_id: Optional[str] = None):
    """Add an immutable outbox row without committing the caller's transaction."""

    payload = canonical_event(event)
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    existing = session.scalar(select(BusinessEventOutbox).where(BusinessEventOutbox.event_id == event.id))
    if existing:
        if existing.payload_hash != payload_hash or existing.payload != payload:
            raise ValueError("business_event_idempotency_conflict")
        return existing
    carrier = {
        key: value
        for key, value in trace_carrier().items()
        if key.lower() in {"traceparent", "tracestate", "baggage"}
    }
    item = BusinessEventOutbox(
        id=str(uuid.uuid4()),
        event_id=event.id,
        event_type=event.type,
        tenant_id=event.tenant_id,
        aggregate_id=aggregate_id,
        payload=payload,
        payload_hash=payload_hash,
        correlation_id=event.correlation_id,
        trace_context=json.dumps(carrier, separators=(",", ":"), sort_keys=True),
    )
    session.add(item)
    return item


def enqueue_named_event(
    session,
    *,
    event_type: str,
    tenant_id: str,
    data: dict,
    correlation_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    event_id: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
):
    identifier = event_id or new_event_id()
    normalized_data = dict(data)
    for key, value in normalized_data.items():
        if key.endswith("_at") and isinstance(value, datetime) and value.tzinfo is None:
            normalized_data[key] = value.replace(tzinfo=timezone.utc)
    event = EventEnvelope(
        id=identifier,
        type=event_type,
        version=1,
        source="klyrow",
        tenant_id=tenant_id,
        correlation_id=correlation_id or identifier,
        causation_id=causation_id,
        occurred_at=occurred_at or utcnow(),
        data=normalized_data,
    )
    return enqueue_event(session, event, aggregate_id=aggregate_id)


def replay_dead_letter(session, event_id: str, *, tenant_id: Optional[str] = None):
    query = select(BusinessEventOutbox).where(BusinessEventOutbox.event_id == event_id)
    if tenant_id is not None:
        query = query.where(BusinessEventOutbox.tenant_id == tenant_id)
    item = session.scalar(query.with_for_update())
    if item is None:
        raise ValueError("business_event_not_found")
    if item.state != "DEAD_LETTER":
        raise ValueError("business_event_not_dead_letter")
    if hashlib.sha256(item.payload.encode("utf-8")).hexdigest() != item.payload_hash:
        raise ValueError("business_event_payload_integrity_failure")
    EventEnvelope.model_validate_json(item.payload)
    item.state = "RETRYING"
    item.attempt_count = 0
    item.next_attempt_at = utcnow()
    item.lease_owner = None
    item.lease_expires_at = None
    item.delivered_at = None
    item.last_error = None
    return item


def daily_snapshot(session, tenant_id: str, day: date):
    """Create a new immutable daily snapshot only when the ledger changed."""
    from .billing import UsageEvent
    from .main import Tenant

    current = utcnow()
    if day >= current.date():
        raise ValueError("usage_day_must_be_closed")
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.set_config("statement_timeout", "5000", True)))
    if session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()) is None:
        raise ValueError("tenant_not_found")
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    quantity = int(session.scalar(select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.unit == "accepted_message",
        UsageEvent.occurred_at >= start,
        UsageEvent.occurred_at < end,
    )))
    aggregate = f"usage:{tenant_id}:{day.isoformat()}"
    prior = session.scalar(select(BusinessEventOutbox).where(
        BusinessEventOutbox.tenant_id == tenant_id,
        BusinessEventOutbox.event_type == "klyrow.usage.daily",
        BusinessEventOutbox.aggregate_id == aggregate,
    ).order_by(BusinessEventOutbox.created_at.desc(), BusinessEventOutbox.id.desc()).limit(1))
    if prior:
        prior_event = EventEnvelope.model_validate_json(prior.payload)
        if prior_event.data["quantity"] == quantity:
            return prior
        previous_time = datetime.fromisoformat(prior_event.data["snapshot_at"].replace("Z", "+00:00"))
        current = max(current, previous_time + timedelta(microseconds=1))
    return enqueue_named_event(
        session,
        event_type="klyrow.usage.daily",
        tenant_id=tenant_id,
        correlation_id=new_event_id(),
        causation_id=aggregate,
        aggregate_id=aggregate,
        occurred_at=current,
        data=DailyUsageData(
            date=day,
            unit="accepted_message",
            quantity=quantity,
            snapshot_at=current,
        ).model_dump(mode="json"),
    )


def snapshot_page(session, day: date, after: str = "", limit: int = 100):
    """Bounded keyset traversal; the caller persists each page before advancing."""
    from .main import Tenant

    if not 1 <= limit <= 100:
        raise ValueError("invalid_snapshot_page_limit")
    tenants = list(session.scalars(
        select(Tenant.id).where(Tenant.id > after).order_by(Tenant.id).limit(limit)
    ))
    for tenant_id in tenants:
        daily_snapshot(session, tenant_id, day)
    return tenants[-1] if len(tenants) == limit else None
