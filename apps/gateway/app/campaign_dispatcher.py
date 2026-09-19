"""Fenced campaign fan-out through the existing Klyrow message admission path."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from prometheus_client import Counter, Gauge
from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, Campaign, Contact, platform_metric


DISPATCH_TOTAL = platform_metric(Counter(
    "klyrow_campaign_dispatch_total",
    "Campaign recipient dispatch results",
    ["codestra_business", "application", "service", "environment", "server", "region", "deployment", "outcome"],
))
DISPATCH_READY = platform_metric(Gauge(
    "klyrow_campaign_dispatch_ready",
    "Campaign recipients ready for message admission",
    ["codestra_business", "application", "service", "environment", "server", "region", "deployment"],
))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return os.getenv("KLYROW_CAMPAIGN_DISPATCHER_ENABLED", "false").strip().lower() == "true"


class CampaignVersionSnapshot(Base):
    __tablename__ = "campaign_version_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    campaign_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sender_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    template_id: Mapped[str] = mapped_column(String(200), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("campaign_id", "campaign_version", name="uq_campaign_version_snapshot"),
    )


class CampaignAudienceSnapshot(Base):
    __tablename__ = "campaign_audience_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    campaign_version: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    recipient_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "campaign_version", "recipient_hash",
            name="uq_campaign_audience_recipient",
        ),
    )


class CampaignDispatchRun(Base):
    __tablename__ = "campaign_dispatch_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    campaign_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="SCHEDULED", index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "state IN ('SCHEDULED','RUNNING','RETRYING','PAUSED','COMPLETED','CANCELLED','FAILED')",
            name="ck_campaign_dispatch_run_state",
        ),
    )


class CampaignDispatchItem(Base):
    __tablename__ = "campaign_dispatch_items"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    campaign_version: Mapped[int] = mapped_column(Integer, nullable=False)
    audience_id: Mapped[str] = mapped_column(String(200), nullable=False)
    recipient_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    fence_token: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "campaign_version", "recipient_hash",
            name="uq_campaign_dispatch_recipient",
        ),
        CheckConstraint(
            "state IN ('PENDING','LEASED','RETRYING','DELIVERED','SUPPRESSED','FAILED','CANCELLED')",
            name="ck_campaign_dispatch_item_state",
        ),
    )


def _audience(session: Session, campaign: Campaign) -> list[str]:
    if campaign.segment_id:
        from .saas import Profile, Segment, match_rule
        segment = session.scalar(select(Segment).where(
            Segment.id == campaign.segment_id,
            Segment.tenant_id == campaign.tenant_id,
        ))
        if segment is None:
            raise HTTPException(422, "campaign_segment_not_found")
        rules = json.loads(segment.rules_json)
        candidates = [
            profile.email for profile in session.scalars(select(Profile).where(
                Profile.tenant_id == campaign.tenant_id,
                Profile.email.is_not(None),
            )).all() if match_rule(session, profile, rules)
        ]
    else:
        candidates = list(session.scalars(select(Contact.email).where(
            Contact.tenant_id == campaign.tenant_id,
            Contact.subscribed.is_(True),
        )))
    return sorted({str(value).strip().lower() for value in candidates if value})


def schedule_campaign(session: Session, campaign: Campaign, scheduled_at: datetime) -> CampaignDispatchRun:
    if not enabled():
        raise HTTPException(409, "campaign_dispatcher_unavailable")
    if campaign.status not in {"draft", "paused"}:
        raise HTTPException(409, "campaign_not_schedulable")
    if not campaign.sender_id or not campaign.template_id:
        raise HTTPException(422, "campaign_sender_and_template_required")
    from .messaging import Template, TemplateVersion
    from .provider import SenderIdentity

    sender = session.scalar(select(SenderIdentity).where(
        SenderIdentity.id == campaign.sender_id,
        SenderIdentity.tenant_id == campaign.tenant_id,
        SenderIdentity.status == "ACTIVE",
        SenderIdentity.stream == "MARKETING",
    ))
    template = session.scalar(select(Template).where(
        Template.id == campaign.template_id,
        Template.tenant_id == campaign.tenant_id,
        Template.status == "PUBLISHED",
    ))
    if sender is None:
        raise HTTPException(422, "campaign_marketing_sender_required")
    if template is None:
        raise HTTPException(422, "campaign_published_template_required")
    template_version = session.scalar(select(TemplateVersion).where(
        TemplateVersion.template_id == template.id,
        TemplateVersion.tenant_id == campaign.tenant_id,
        TemplateVersion.version == template.current_version,
    ))
    if template_version is None:
        raise HTTPException(409, "campaign_template_version_missing")
    existing = session.scalar(select(CampaignDispatchRun).where(
        CampaignDispatchRun.campaign_id == campaign.id
    ))
    if existing is not None:
        return existing
    version = campaign.current_version
    snapshot = CampaignVersionSnapshot(
        id=str(uuid.uuid4()), tenant_id=campaign.tenant_id, campaign_id=campaign.id,
        campaign_version=version, sender_id=sender.id, sender=sender.email,
        template_id=template.id, template_version=template.current_version,
        subject=campaign.subject or template_version.subject,
        html_body=template_version.html_body, text_body=template_version.text_body,
    )
    session.add(snapshot)
    for recipient in _audience(session, campaign):
        digest = hashlib.sha256(recipient.encode("utf-8")).hexdigest()
        audience = CampaignAudienceSnapshot(
            id=str(uuid.uuid4()), tenant_id=campaign.tenant_id, campaign_id=campaign.id,
            campaign_version=version, recipient=recipient, recipient_hash=digest,
        )
        session.add(audience)
        session.add(CampaignDispatchItem(
            id=str(uuid.uuid4()), tenant_id=campaign.tenant_id, campaign_id=campaign.id,
            campaign_version=version, audience_id=audience.id, recipient_hash=digest,
            next_attempt_at=scheduled_at,
        ))
    run = CampaignDispatchRun(
        id=str(uuid.uuid4()), tenant_id=campaign.tenant_id, campaign_id=campaign.id,
        campaign_version=version, scheduled_at=scheduled_at, next_attempt_at=scheduled_at,
    )
    session.add(run)
    campaign.status = "scheduled"
    campaign.scheduled_at = scheduled_at
    return run


def pause_campaign(session: Session, campaign: Campaign) -> CampaignDispatchRun:
    run = session.scalar(select(CampaignDispatchRun).where(
        CampaignDispatchRun.campaign_id == campaign.id,
        CampaignDispatchRun.tenant_id == campaign.tenant_id,
    ).with_for_update())
    if run is None or run.state in {"COMPLETED", "CANCELLED", "FAILED"}:
        raise HTTPException(409, "campaign_not_pauseable")
    run.state = "PAUSED"
    run.lease_owner = None
    run.lease_expires_at = None
    for item in session.scalars(select(CampaignDispatchItem).where(
        CampaignDispatchItem.campaign_id == campaign.id,
        CampaignDispatchItem.state == "LEASED",
    )).all():
        item.state = "RETRYING"
        item.lease_owner = None
        item.fence_token = None
        item.next_attempt_at = utcnow()
    campaign.status = "paused"
    return run


def resume_campaign(session: Session, campaign: Campaign) -> CampaignDispatchRun:
    run = session.scalar(select(CampaignDispatchRun).where(
        CampaignDispatchRun.campaign_id == campaign.id,
        CampaignDispatchRun.tenant_id == campaign.tenant_id,
    ).with_for_update())
    if run is None or run.state != "PAUSED":
        raise HTTPException(409, "campaign_not_resumable")
    run.state = "RETRYING"
    run.next_attempt_at = utcnow()
    campaign.status = "scheduled"
    return run


def _counts(session: Session, run: CampaignDispatchRun) -> dict[str, int]:
    values = {}
    for state in ("DELIVERED", "SUPPRESSED", "FAILED", "CANCELLED"):
        values[state] = int(session.scalar(select(func.count()).select_from(CampaignDispatchItem).where(
            CampaignDispatchItem.campaign_id == run.campaign_id,
            CampaignDispatchItem.campaign_version == run.campaign_version,
            CampaignDispatchItem.state == state,
        )) or 0)
    values["TOTAL"] = int(session.scalar(select(func.count()).select_from(CampaignDispatchItem).where(
        CampaignDispatchItem.campaign_id == run.campaign_id,
        CampaignDispatchItem.campaign_version == run.campaign_version,
    )) or 0)
    return values


def _summary_event(session: Session, run: CampaignDispatchRun, status: str) -> None:
    from .business_events import enqueue_named_event
    counts = _counts(session, run)
    event_id = "evt_" + uuid.uuid5(
        uuid.NAMESPACE_URL, f"klyrow:campaign:{run.campaign_id}:{run.campaign_version}:{status}"
    ).hex
    enqueue_named_event(
        session,
        event_type="klyrow.campaign.summary",
        tenant_id=run.tenant_id,
        event_id=event_id,
        correlation_id="cmp_" + run.campaign_id,
        causation_id=run.id,
        aggregate_id=run.campaign_id,
        data={
            "campaign_id": run.campaign_id,
            "campaign_version": run.campaign_version,
            "status": status,
            "audience_count": counts["TOTAL"],
            "delivered_count": counts["DELIVERED"],
            "suppressed_count": counts["SUPPRESSED"],
            "failed_count": counts["FAILED"],
        },
    )


def cancel_campaign(session: Session, campaign: Campaign) -> Optional[CampaignDispatchRun]:
    run = session.scalar(select(CampaignDispatchRun).where(
        CampaignDispatchRun.campaign_id == campaign.id,
        CampaignDispatchRun.tenant_id == campaign.tenant_id,
    ).with_for_update())
    campaign.status = "cancelled"
    campaign.scheduled_at = None
    if run is None:
        return None
    if run.state in {"COMPLETED", "CANCELLED"}:
        return run
    run.state = "CANCELLED"
    run.completed_at = utcnow()
    run.lease_owner = None
    run.lease_expires_at = None
    for item in session.scalars(select(CampaignDispatchItem).where(
        CampaignDispatchItem.campaign_id == campaign.id,
        CampaignDispatchItem.state.in_(("PENDING", "LEASED", "RETRYING")),
    )).all():
        item.state = "CANCELLED"
        item.lease_owner = None
        item.fence_token = None
    _summary_event(session, run, "CANCELLED")
    return run


def _claim_run(session: Session, now: datetime, lease_seconds: int) -> Optional[CampaignDispatchRun]:
    expired = session.scalar(select(CampaignDispatchRun).where(
        CampaignDispatchRun.state == "RUNNING",
        CampaignDispatchRun.lease_expires_at < now,
    ).order_by(CampaignDispatchRun.scheduled_at).with_for_update(skip_locked=True).limit(1))
    if expired is not None:
        expired.state = "RETRYING"
        expired.next_attempt_at = now
        expired.lease_owner = None
        expired.lease_expires_at = None
        for item in session.scalars(select(CampaignDispatchItem).where(
            CampaignDispatchItem.campaign_id == expired.campaign_id,
            CampaignDispatchItem.state == "LEASED",
        )).all():
            item.state = "RETRYING"
            item.lease_owner = None
            item.fence_token = None
            item.next_attempt_at = now
        session.flush()
    run = session.scalar(select(CampaignDispatchRun).where(
        CampaignDispatchRun.state.in_(("SCHEDULED", "RETRYING")),
        CampaignDispatchRun.scheduled_at <= now,
        CampaignDispatchRun.next_attempt_at <= now,
    ).order_by(CampaignDispatchRun.scheduled_at, CampaignDispatchRun.id)
      .with_for_update(skip_locked=True).limit(1))
    if run is None:
        return None
    run.state = "RUNNING"
    run.fence_token += 1
    run.lease_owner = "cmpw_" + uuid.uuid4().hex
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    session.commit()
    return run


def _mark_result(
    session: Session,
    *,
    item_id: str,
    owner: str,
    fence: int,
    state: str,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    item = session.scalar(select(CampaignDispatchItem).where(
        CampaignDispatchItem.id == item_id,
        CampaignDispatchItem.state == "LEASED",
        CampaignDispatchItem.lease_owner == owner,
        CampaignDispatchItem.fence_token == fence,
    ).with_for_update())
    if item is None:
        return False
    item.state = state
    item.message_id = message_id
    item.last_error = error
    item.lease_owner = None
    item.fence_token = None
    if state == "RETRYING":
        item.next_attempt_at = utcnow() + timedelta(seconds=min(900, 2 ** item.attempt_count))
    session.commit()
    DISPATCH_TOTAL.labels(state.lower()).inc()
    return True


def dispatch_campaigns(limit: int = 100) -> int:
    """Process one bounded chunk; provider admission remains authoritative."""
    from .main import DB
    if not enabled():
        return 0
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid_campaign_dispatch_limit")
    lease_seconds = int(os.getenv("KLYROW_CAMPAIGN_DISPATCH_LEASE_SECONDS", "120"))
    if not 10 <= lease_seconds <= 3600:
        raise ValueError("invalid_campaign_dispatch_lease")
    current = utcnow()
    with DB() as session:
        run = _claim_run(session, current, lease_seconds)
        if run is None:
            DISPATCH_READY.set(0)
            return 0
        run_id, campaign_id, tenant_id = run.id, run.campaign_id, run.tenant_id
        version, owner, fence = run.campaign_version, run.lease_owner, run.fence_token
        snapshot = session.scalar(select(CampaignVersionSnapshot).where(
            CampaignVersionSnapshot.campaign_id == campaign_id,
            CampaignVersionSnapshot.campaign_version == version,
            CampaignVersionSnapshot.tenant_id == tenant_id,
        ))
        items = list(session.scalars(select(CampaignDispatchItem).where(
            CampaignDispatchItem.campaign_id == campaign_id,
            CampaignDispatchItem.campaign_version == version,
            CampaignDispatchItem.state.in_(("PENDING", "RETRYING")),
            CampaignDispatchItem.next_attempt_at <= current,
        ).order_by(CampaignDispatchItem.created_at, CampaignDispatchItem.id)
          .with_for_update(skip_locked=True).limit(limit)))
        if snapshot is None:
            run.state = "FAILED"
            run.completed_at = current
            session.commit()
            return 0
        snapshots = []
        for item in items:
            audience = session.get(CampaignAudienceSnapshot, item.audience_id)
            if audience is None or audience.tenant_id != tenant_id:
                item.state = "FAILED"
                item.last_error = "campaign_audience_snapshot_missing"
                continue
            item.state = "LEASED"
            item.attempt_count += 1
            item.lease_owner = owner
            item.fence_token = fence
            snapshots.append((item.id, audience.recipient, item.recipient_hash))
        session.commit()
    processed = 0
    for item_id, recipient, recipient_hash in snapshots:
        state, message_id, error = "FAILED", None, None
        with DB() as session:
            active = session.scalar(select(CampaignDispatchRun.id).where(
                CampaignDispatchRun.id == run_id,
                CampaignDispatchRun.state == "RUNNING",
                CampaignDispatchRun.lease_owner == owner,
                CampaignDispatchRun.fence_token == fence,
            ).with_for_update())
            if active is None:
                continue
            try:
                from .guards import enforce_suppression
                from .provider import ProviderMailIn, email_send
                enforce_suppression(session, tenant_id, recipient, "MARKETING", campaign_id)
                sandbox = os.getenv("KLYROW_CAMPAIGN_DISPATCHER_SANDBOX", "true").lower() == "true"
                if not sandbox and os.getenv("KLYROW_CAMPAIGN_DISPATCHER_ALLOW_LIVE", "false").lower() != "true":
                    raise HTTPException(503, "campaign_live_delivery_not_authorized")
                payload = ProviderMailIn(
                    sender=snapshot.sender, recipient=recipient, subject=snapshot.subject,
                    html=snapshot.html_body or None, text=snapshot.text_body or None,
                    stream="MARKETING", sandbox=sandbox,
                )
                result = email_send(
                    payload,
                    {"tenant": tenant_id, "sub": "campaign-dispatcher", "role": "tenant_admin", "service": "campaign-dispatcher"},
                    session,
                    f"campaign:{campaign_id}:{version}:{recipient_hash}",
                    "cmpmsg_" + hashlib.sha256(f"{campaign_id}:{version}:{recipient_hash}".encode()).hexdigest()[:32],
                )
                state, message_id = "DELIVERED", result["message_id"]
            except HTTPException as exc:
                if exc.status_code == 422 and exc.detail in {"recipient_suppressed", "marketing_consent_required"}:
                    state, error = "SUPPRESSED", str(exc.detail)
                elif exc.status_code in {408, 429, 500, 502, 503, 504}:
                    state, error = "RETRYING", f"message_admission_{exc.status_code}"
                else:
                    state, error = "FAILED", f"message_admission_{exc.status_code}"
            if _mark_result(session, item_id=item_id, owner=owner, fence=fence, state=state,
                            message_id=message_id, error=error):
                processed += 1
    with DB() as session:
        run = session.scalar(select(CampaignDispatchRun).where(
            CampaignDispatchRun.id == run_id,
            CampaignDispatchRun.state == "RUNNING",
            CampaignDispatchRun.lease_owner == owner,
            CampaignDispatchRun.fence_token == fence,
        ).with_for_update())
        if run is None:
            return processed
        remaining = int(session.scalar(select(func.count()).select_from(CampaignDispatchItem).where(
            CampaignDispatchItem.campaign_id == campaign_id,
            CampaignDispatchItem.campaign_version == version,
            CampaignDispatchItem.state.in_(("PENDING", "LEASED", "RETRYING")),
        )) or 0)
        run.lease_owner = None
        run.lease_expires_at = None
        if remaining:
            run.state = "RETRYING"
            run.next_attempt_at = utcnow() + timedelta(seconds=2)
        else:
            run.state = "COMPLETED"
            run.completed_at = utcnow()
            campaign = session.scalar(select(Campaign).where(
                Campaign.id == campaign_id, Campaign.tenant_id == tenant_id
            ).with_for_update())
            if campaign is not None:
                campaign.status = "completed"
                campaign.scheduled_at = None
            _summary_event(session, run, "COMPLETED")
        DISPATCH_READY.set(remaining)
        session.commit()
    return processed
