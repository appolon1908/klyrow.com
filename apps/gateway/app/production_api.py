"""Canonical Klyrow production API surface.

This module fills the product-control routes that are not aliases of an
established Klyrow route.  Provider mutations are represented by the durable
integration outbox; this module never performs a browser-request provider call.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import (
    Boolean,
    DateTime,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .billing import BillingPlan, BillingPrice, BillingSubscription, Wallet
from .capabilities import (
    has_permission as _has_permission,
    require_permission as _require_permission,
)
from .main import (
    Base,
    Contact,
    Domain,
    EmailOutbox,
    Event,
    Idempotency,
    Message,
    MiddlewareCommandOperation,
    Suppression,
    Tenant,
    User,
    audit,
    auth,
    db,
    scoped_idempotency_key,
    semantic_request_hash,
)
from .messaging import (
    Template,
    TemplateUpdate,
    TemplateVersion,
    template_update,
    validate_html,
)
from .mautic_contract import SUPPORTED_MAUTIC_COMMANDS
from .operations import (
    IntegrationOutbox,
    IntegrationResult,
    require_safe_integration_recovery,
)
from .durable_results import (
    read_control_response,
    seal_control_response,
    result_readback,
)
from .tenancy import (
    Organization,
    ROLE_PERMISSIONS,
    RoleIn,
    TenantMember,
    manage,
    role_change,
)


router = APIRouter(tags=["Production API"])
now = lambda: datetime.now(timezone.utc)


class ContactList(Base):
    __tablename__ = "contact_lists"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_contact_list_tenant_name"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TemplateVersionView(BaseModel):
    id: str
    template_id: str
    version: int
    subject: str
    html_body: str
    text_body: str
    variables: list[str]
    created_at: datetime


class TemplateVersionPage(BaseModel):
    items: list[TemplateVersionView]
    next_cursor: str | None


def _version_view(item: TemplateVersion) -> TemplateVersionView:
    created_at = item.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return TemplateVersionView(
        id=item.id,
        template_id=item.template_id,
        version=item.version,
        subject=item.subject,
        html_body=item.html_body,
        text_body=item.text_body,
        variables=json.loads(item.variables_json),
        created_at=created_at.astimezone(timezone.utc),
    )


def _owned_template(session: Session, tenant: str, template_id: str) -> None:
    if (
        session.scalar(
            select(Template.id).where(
                Template.id == template_id, Template.tenant_id == tenant
            )
        )
        is None
    ):
        raise HTTPException(404, "template_not_found")


@router.get(
    "/v1/templates/{template_id}/versions",
    response_model=TemplateVersionPage,
    responses={
        401: {"description": "Authentication required"},
        404: {"description": "Template not found"},
        422: {"description": "Invalid cursor or limit"},
    },
)
def template_versions(
    template_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=64),
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> TemplateVersionPage:
    _owned_template(s, ctx["tenant"], template_id)
    query = select(TemplateVersion).where(
        TemplateVersion.tenant_id == ctx["tenant"],
        TemplateVersion.template_id == template_id,
    )
    if cursor is not None:
        try:
            raw = base64.b64decode(cursor, altchars=b"-_", validate=True).decode(
                "ascii"
            )
            if (
                not raw.isascii()
                or not raw.isdecimal()
                or len(raw) > 10
                or int(raw) < 1
            ):
                raise ValueError("invalid version cursor")
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise HTTPException(422, "invalid_cursor") from exc
        query = query.where(TemplateVersion.version < int(raw))
    items = s.scalars(
        query.order_by(TemplateVersion.version.desc()).limit(limit + 1)
    ).all()
    page = items[:limit]
    next_cursor = None
    if len(items) > limit:
        next_cursor = base64.urlsafe_b64encode(
            str(page[-1].version).encode("ascii")
        ).decode("ascii")
    return TemplateVersionPage(
        items=[_version_view(item) for item in page], next_cursor=next_cursor
    )


@router.get(
    "/v1/templates/{template_id}/versions/{version_id}",
    response_model=TemplateVersionView,
    responses={
        401: {"description": "Authentication required"},
        404: {"description": "Template version not found"},
    },
)
def template_version(
    template_id: str,
    version_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> TemplateVersionView:
    _owned_template(s, ctx["tenant"], template_id)
    item = s.scalar(
        select(TemplateVersion).where(
            TemplateVersion.id == version_id,
            TemplateVersion.template_id == template_id,
            TemplateVersion.tenant_id == ctx["tenant"],
        )
    )
    if item is None:
        raise HTTPException(404, "template_version_not_found")
    return _version_view(item)


class MemberIn(BaseModel):
    user_id: str
    role: str


class DomainPatch(BaseModel):
    domain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]+$")


class ContactPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    subscribed: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)


class ListPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    active: Optional[bool] = None


class CampaignPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    subject: Optional[str] = Field(default=None, max_length=998)


class CampaignDispatchConfiguration(BaseModel):
    sender_id: str = Field(min_length=1, max_length=200)
    template_id: str = Field(min_length=1, max_length=200)
    segment_id: Optional[str] = Field(default=None, max_length=200)


class CampaignSchedule(BaseModel):
    scheduled_at: datetime


class SuppressionIn(BaseModel):
    email: EmailStr
    reason: str = Field(default="policy", min_length=2, max_length=120)


class MauticCommand(BaseModel):
    command: str = Field(min_length=1, max_length=120)
    aggregate_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(min_length=8, max_length=200)
    operation_id: Optional[str] = None
    timestamp: datetime
    trace_context: Optional[str] = Field(default=None, max_length=512)

    @field_validator("command")
    @classmethod
    def command_is_implemented(cls, value: str) -> str:
        if value not in SUPPORTED_MAUTIC_COMMANDS:
            raise ValueError("mautic_command_unsupported")
        return value


def _tenant_item(s: Session, model: Any, item_id: str, tenant_id: str) -> Any:
    item = s.scalar(
        select(model).where(model.id == item_id, model.tenant_id == tenant_id)
    )
    if item is None:
        raise HTTPException(404, "not_found")
    return item


def _tenant_item_for_update(
    s: Session, model: Any, item_id: str, tenant_id: str
) -> Any:
    item = s.scalar(
        select(model)
        .where(model.id == item_id, model.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise HTTPException(404, "not_found")
    return item


def _idempotency_begin(
    s: Session,
    ctx: dict[str, Any],
    raw_key: str,
    *,
    action: str,
    resource: str,
    semantic_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, str]:
    if not isinstance(raw_key, str) or not 8 <= len(raw_key) <= 200:
        raise HTTPException(400, "idempotency_key_required")
    storage_key = scoped_idempotency_key(
        ctx, raw_key, action=action, resource=resource, api_version="v1"
    )
    request_hash = semantic_request_hash(
        action=action, resource=resource, payload=semantic_payload, api_version="v1"
    )
    prior = s.scalar(
        select(Idempotency).where(
            Idempotency.tenant_id == ctx["tenant"], Idempotency.key == storage_key
        )
    )
    if prior is None:
        return None, storage_key, request_hash
    if prior.request_hash != request_hash:
        raise HTTPException(409, "idempotency_key_payload_mismatch")
    response = read_control_response(prior)
    return response, storage_key, request_hash


def _idempotency_complete(
    s: Session,
    ctx: dict[str, Any],
    *,
    storage_key: str,
    request_hash: str,
    resource: str,
    response: dict[str, Any],
) -> None:
    s.add(
        Idempotency(
            key=storage_key,
            tenant_id=ctx["tenant"],
            request_hash=request_hash,
            resource_id=resource,
            response_json=seal_control_response(
                jsonable_encoder(response),
                tenant_id=ctx["tenant"],
                storage_key=storage_key,
                request_hash=request_hash,
                resource_id=resource,
            ),
        )
    )


def _operation_json(
    item: MiddlewareCommandOperation | IntegrationOutbox,
    s: Session,
    *,
    results: dict | None = None,
) -> dict[str, Any]:
    if isinstance(item, MiddlewareCommandOperation):
        state = {
            "accepted": "QUEUED",
            "queued": "QUEUED",
            "completed": "SUCCEEDED",
            "cancelled": "CANCELLED",
            "failed": "FAILED",
        }.get(item.state, item.state.upper())
        return {
            "operation_id": item.command_id,
            "status": state,
            "result": json.loads(item.result_json or "{}"),
            "error": item.error,
            "retryability": state in {"FAILED", "RECONCILIATION_REQUIRED"},
            "reconciliation_required": state == "RECONCILIATION_REQUIRED",
            "correlation_id": item.correlation_id,
            "resource_version": item.updated_at.isoformat(),
        }
    state = {
        "PENDING": "QUEUED",
        "PROCESSING": "PROCESSING",
        "COMPLETED": "SUCCEEDED",
        "RETRY": "FAILED",
        "DEAD_LETTER": "RECONCILIATION_REQUIRED",
        "CANCELLED": "CANCELLED",
    }.get(item.state, item.state)
    if results is not None:
        persisted_result, late_observation = results[(item.tenant_id, item.id)]
    else:
        persisted_result = None
        if item.state == "COMPLETED":
            persisted_result = s.scalar(
                select(IntegrationResult)
                .where(
                    IntegrationResult.outbox_id == item.id,
                    IntegrationResult.tenant_id == item.tenant_id,
                    IntegrationResult.source == item.target,
                )
                .order_by(
                    IntegrationResult.created_at.desc(), IntegrationResult.id.desc()
                )
            )
        late_observation = (
            item.target == "MAUTIC"
            and s.scalar(
                select(IntegrationResult.id)
                .where(
                    IntegrationResult.outbox_id == item.id,
                    IntegrationResult.tenant_id == item.tenant_id,
                    IntegrationResult.source == "MAUTIC_LATE",
                )
                .limit(1)
            )
            is not None
        )
    result, result_metadata = result_readback(persisted_result)
    missing_result = item.state == "COMPLETED" and result_metadata[
        "availability"
    ] not in {"AVAILABLE", "PURGED"}
    return {
        "operation_id": item.id,
        "status": state,
        "result": result,
        "result_metadata": result_metadata,
        "error": item.last_error
        or ("operation_result_unavailable" if missing_result else None),
        "retryability": item.state == "RETRY" and not late_observation,
        "reconciliation_required": item.state == "DEAD_LETTER"
        or missing_result
        or late_observation,
        # The runtime envelope adapter restores a real correlation identifier.
        # A storage/idempotency digest is never a correlation identifier.
        "correlation_id": None,
        "resource_version": item.updated_at.isoformat(),
    }


def _operations_json(items: list, s: Session) -> list[dict[str, Any]]:
    """At most two result queries per tenant, regardless of page size."""
    groups: dict[str, list[IntegrationOutbox]] = {}
    snapshots: dict[tuple[str, str], tuple[IntegrationResult | None, bool]] = {}
    for item in items:
        if isinstance(item, IntegrationOutbox):
            groups.setdefault(item.tenant_id, []).append(item)
            snapshots[(item.tenant_id, item.id)] = (None, False)
    for tenant_id, rows in groups.items():
        mautic_ids = [row.id for row in rows if row.target == "MAUTIC"]
        if mautic_ids:
            late_ids = s.scalars(
                select(IntegrationResult.outbox_id)
                .where(
                    IntegrationResult.tenant_id == tenant_id,
                    IntegrationResult.outbox_id.in_(mautic_ids),
                    IntegrationResult.source == "MAUTIC_LATE",
                )
                .distinct()
            ).all()
            for outbox_id in late_ids:
                snapshots[(tenant_id, outbox_id)] = (None, True)
        completed_ids = [row.id for row in rows if row.state == "COMPLETED"]
        if completed_ids:
            # Bound the fetched rows to one latest, correctly attributed result
            # per operation rather than loading an unbounded history per page.
            ranked = (
                select(
                    IntegrationResult.id.label("result_id"),
                    func.row_number()
                    .over(
                        partition_by=(
                            IntegrationResult.tenant_id,
                            IntegrationResult.outbox_id,
                        ),
                        order_by=(
                            IntegrationResult.created_at.desc(),
                            IntegrationResult.id.desc(),
                        ),
                    )
                    .label("position"),
                )
                .join(
                    IntegrationOutbox,
                    (
                        (IntegrationResult.outbox_id == IntegrationOutbox.id)
                        & (IntegrationResult.tenant_id == IntegrationOutbox.tenant_id)
                        & (IntegrationResult.source == IntegrationOutbox.target)
                    ),
                )
                .where(
                    IntegrationResult.tenant_id == tenant_id,
                    IntegrationResult.outbox_id.in_(completed_ids),
                )
                .subquery()
            )
            latest = s.scalars(
                select(IntegrationResult)
                .join(
                    ranked,
                    IntegrationResult.id == ranked.c.result_id,
                )
                .where(ranked.c.position == 1)
            ).all()
            for result in latest:
                identity = (result.tenant_id, result.outbox_id)
                snapshots[identity] = (result, snapshots[identity][1])
    return [_operation_json(item, s, results=snapshots) for item in items]


def _find_operation(s: Session, operation_id: str, tenant_id: str) -> Any:
    item = s.scalar(
        select(MiddlewareCommandOperation).where(
            MiddlewareCommandOperation.command_id == operation_id,
            MiddlewareCommandOperation.tenant_id == tenant_id,
        )
    )
    if item is None:
        item = s.scalar(
            select(IntegrationOutbox).where(
                IntegrationOutbox.id == operation_id,
                IntegrationOutbox.tenant_id == tenant_id,
            )
        )
    if item is None:
        raise HTTPException(404, "not_found")
    return item


def _find_operation_for_update(s: Session, operation_id: str, tenant_id: str) -> Any:
    item = s.scalar(
        select(MiddlewareCommandOperation)
        .where(
            MiddlewareCommandOperation.command_id == operation_id,
            MiddlewareCommandOperation.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        item = s.scalar(
            select(IntegrationOutbox)
            .where(
                IntegrationOutbox.id == operation_id,
                IntegrationOutbox.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if item is None:
        raise HTTPException(404, "not_found")
    return item


def _mautic_permission(command: str) -> str:
    if command in {"sync.request.v1", "form_submissions.read.v1"}:
        return "analytics.read"
    if command == "webhook.register.v1":
        return "webhook.manage"
    if command.startswith("campaign.") or command.startswith("campaign_membership."):
        return "campaign.manage"
    if command == "email_campaign.state.v1":
        return "campaign.manage"
    return "contact.manage"


def _require_mautic_permission(ctx: dict[str, Any], command: str) -> None:
    if not _has_permission(ctx, _mautic_permission(command)):
        raise HTTPException(403, "mautic_command_permission_denied")


def _authorize_operation_mutation(ctx: dict[str, Any], item: Any) -> None:
    if _has_permission(ctx, "klyrow.middleware.operation.write"):
        return
    if isinstance(item, MiddlewareCommandOperation):
        permission = {
            "email.message.send.v1": "mail.send",
            "email.message.cancel.v1": "mail.send",
            "email.domain.verify.v1": "domain.manage",
            "email.suppression.upsert.v1": "contact.manage",
            "email.reputation.snapshot.request.v1": "analytics.read",
        }.get(item.command)
    elif isinstance(item, IntegrationOutbox) and item.target == "MAUTIC":
        _require_mautic_permission(ctx, item.event_type)
        return
    elif isinstance(item, IntegrationOutbox) and item.target == "N8N":
        permission = "webhook.manage"
    elif (
        isinstance(item, IntegrationOutbox)
        and item.target == "MIDDLEWARE_OBSERVABILITY"
    ):
        permission = "klyrow.observability.write"
    elif isinstance(item, IntegrationOutbox) and item.target == "ODOO":
        permission = (
            "support.manage"
            if item.event_type == "SupportTicketCreatedV1"
            else "billing.manage"
        )
    else:
        permission = None
    if permission is None:
        raise HTTPException(403, "operation_mutation_permission_denied")
    _require_permission(ctx, permission)


def _authorize_operation_read(ctx: dict[str, Any], item: Any) -> None:
    """Apply the command-specific permission to Mautic result visibility too."""
    if isinstance(item, IntegrationOutbox) and item.target == "MAUTIC":
        _require_mautic_permission(ctx, item.event_type)


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "live"}


@router.get(
    "/health/ready",
    responses={503: {"description": "Application database unavailable"}},
)
def health_ready(s: Session = Depends(db)) -> dict[str, str]:
    try:
        s.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # Report admission readiness without exposing SQL, connection strings,
        # credentials or dependency exception text to the public health route.
        raise HTTPException(503, "database_unavailable") from exc
    return {"status": "ready", "database": "ok"}


@router.get("/v1/me/permissions")
def my_permissions(
    ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    membership = s.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == ctx["tenant"],
            TenantMember.user_id == ctx["sub"],
            TenantMember.active == True,
        )
    )
    role = (membership.role if membership else ctx.get("role", "READ_ONLY")).upper()
    return {"role": role, "permissions": sorted(ROLE_PERMISSIONS.get(role, set()))}


@router.get("/v1/me/capabilities")
def my_capabilities(ctx: dict = Depends(auth)) -> dict[str, Any]:
    return {
        "tenant_id": ctx["tenant"],
        "capabilities": [
            "email",
            "domains",
            "templates",
            "contacts",
            "lists",
            "campaigns",
            "operations",
            "postal",
            "mautic",
        ],
    }


@router.get("/v1/me/sessions")
def my_sessions(ctx: dict = Depends(auth), s: Session = Depends(db)) -> dict[str, Any]:
    from .saas import SessionRecord

    rows = s.scalars(
        select(SessionRecord).where(
            SessionRecord.user_id == ctx["sub"], SessionRecord.revoked == False
        )
    ).all()
    return {"items": rows}


@router.get("/v1/organizations/{organization_id}")
def organization_detail(
    organization_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    item = s.get(Organization, organization_id)
    if item is None or not s.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == item.tenant_id,
            TenantMember.user_id == ctx["sub"],
            TenantMember.active == True,
        )
    ):
        raise HTTPException(404, "not_found")
    return item


@router.get("/v1/organizations/{organization_id}/members")
def organization_members(
    organization_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    item = organization_detail(organization_id, ctx, s)
    rows = s.execute(
        select(TenantMember, User)
        .join(User, User.id == TenantMember.user_id)
        .where(TenantMember.tenant_id == item.tenant_id, TenantMember.active == True)
    ).all()
    return {
        "items": [
            {
                "id": member.id,
                "user_id": user.id,
                "email": user.email,
                "role": member.role,
            }
            for member, user in rows
        ]
    }


@router.post("/v1/organizations/{organization_id}/members", status_code=201)
def organization_member_add(
    organization_id: str,
    body: MemberIn,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> dict[str, Any]:
    organization = organization_detail(organization_id, ctx, s)
    manage({**ctx, "tenant": organization.tenant_id}, s)
    user = s.get(User, body.user_id)
    if user is None:
        raise HTTPException(404, "user_not_found")
    role = body.role.upper()
    if role not in ROLE_PERMISSIONS:
        raise HTTPException(422, "invalid_tenant_role")
    item = s.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == organization.tenant_id,
            TenantMember.user_id == user.id,
        )
    )
    if item is None:
        item = TenantMember(
            id=str(uuid.uuid4()),
            tenant_id=organization.tenant_id,
            user_id=user.id,
            role=role,
        )
    item.role = role
    item.active = True
    s.add(item)
    audit(s, {**ctx, "tenant": organization.tenant_id}, "tenant.member.added")
    s.commit()
    return {"id": item.id, "user_id": item.user_id, "role": item.role}


@router.patch("/v1/organizations/{organization_id}/members/{member_id}")
def organization_member_patch(
    organization_id: str,
    member_id: str,
    body: RoleIn,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> dict[str, Any]:
    organization = organization_detail(organization_id, ctx, s)
    item = s.scalar(
        select(TenantMember).where(
            TenantMember.id == member_id,
            TenantMember.tenant_id == organization.tenant_id,
        )
    )
    if item is None:
        raise HTTPException(404, "member_not_found")
    return role_change(item.user_id, body, {**ctx, "tenant": organization.tenant_id}, s)


@router.get("/v1/domains/{domain_id}")
def domain_detail(
    domain_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    return _tenant_item(s, Domain, domain_id, ctx["tenant"])


@router.patch("/v1/domains/{domain_id}")
def domain_patch(
    domain_id: str,
    body: DomainPatch,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> Any:
    manage(ctx, s)
    item = _tenant_item(s, Domain, domain_id, ctx["tenant"])
    item.domain = body.domain.lower().rstrip(".")
    item.verified = False
    audit(s, ctx, "domain.updated")
    s.commit()
    return item


@router.delete("/v1/domains/{domain_id}", status_code=204)
def domain_delete(
    domain_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Response:
    manage(ctx, s)
    item = _tenant_item(s, Domain, domain_id, ctx["tenant"])
    if item.verified:
        raise HTTPException(409, "verified_domain_must_be_disabled_before_delete")
    s.delete(item)
    audit(s, ctx, "domain.deleted")
    s.commit()
    return Response(status_code=204)


@router.get("/v1/domains/{domain_id}/dns")
def domain_dns(
    domain_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    item = _tenant_item(s, Domain, domain_id, ctx["tenant"])
    return {
        "domain_id": item.id,
        "records": [
            {
                "type": "TXT",
                "name": "_klyrow-verification." + item.domain,
                "value": "klyrow=" + item.token,
            }
        ],
    }


@router.get("/v1/domains/{domain_id}/verification")
def domain_verification(
    domain_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    item = _tenant_item(s, Domain, domain_id, ctx["tenant"])
    return {
        "domain_id": item.id,
        "verified": item.verified,
        "status": "VERIFIED" if item.verified else "DNS_REQUIRED",
    }


@router.post("/v1/messages/{message_id}/cancel")
def message_cancel(
    message_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    _require_permission(ctx, "mail.send")
    item = _tenant_item_for_update(s, Message, message_id, ctx["tenant"])
    prior, storage_key, request_hash = _idempotency_begin(
        s,
        ctx,
        idempotency_key,
        action="message.cancel",
        resource=message_id,
        semantic_payload={},
    )
    if prior is not None:
        return prior
    if item.status in {"delivered", "bounced", "complained", "failed", "cancelled"}:
        raise HTTPException(409, "terminal_message_cannot_cancel")
    outbox = s.scalar(
        select(EmailOutbox).where(
            EmailOutbox.tenant_id == ctx["tenant"], EmailOutbox.message_id == item.id
        )
    )
    if outbox and outbox.state not in {"pending", "retry"}:
        raise HTTPException(409, "provider_submission_requires_reconciliation")
    if outbox:
        outbox.state = "cancelled"
        outbox.last_error = "cancelled_by_api"
        outbox.updated_at = now()
    item.status = "cancelled"
    audit(s, ctx, "message.cancelled")
    result = {"id": item.id, "status": item.status}
    _idempotency_complete(
        s,
        ctx,
        storage_key=storage_key,
        request_hash=request_hash,
        resource=message_id,
        response=result,
    )
    s.commit()
    return result


@router.get("/v1/templates/{template_id}")
def template_detail(
    template_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    item = _tenant_item(s, Template, template_id, ctx["tenant"])
    version = s.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == item.id,
            TemplateVersion.version == item.current_version,
            TemplateVersion.tenant_id == ctx["tenant"],
        )
    )
    return {"template": item, "version": version}


@router.patch("/v1/templates/{template_id}")
def template_patch(
    template_id: str,
    body: TemplateUpdate,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> dict[str, Any]:
    return template_update(template_id, body, ctx, s)


@router.delete("/v1/templates/{template_id}", status_code=204)
def template_delete(
    template_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Response:
    _require_permission(ctx, "template.manage")
    item = _tenant_item(s, Template, template_id, ctx["tenant"])
    if item.status == "PUBLISHED":
        raise HTTPException(409, "published_template_cannot_be_deleted")
    for version in s.scalars(
        select(TemplateVersion).where(
            TemplateVersion.template_id == item.id,
            TemplateVersion.tenant_id == ctx["tenant"],
        )
    ).all():
        s.delete(version)
    s.delete(item)
    audit(s, ctx, "template.deleted")
    s.commit()
    return Response(status_code=204)


@router.get("/v1/contacts/{contact_id}")
def contact_detail(
    contact_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    return _tenant_item(s, Contact, contact_id, ctx["tenant"])


@router.patch("/v1/contacts/{contact_id}")
def contact_patch(
    contact_id: str,
    body: ContactPatch,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> Any:
    _require_permission(ctx, "contact.manage")
    item = _tenant_item(s, Contact, contact_id, ctx["tenant"])
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(
            item,
            "metadata_json" if key == "metadata" else key,
            json.dumps(value, sort_keys=True) if key == "metadata" else value,
        )
    audit(s, ctx, "contact.updated")
    s.commit()
    return item


@router.delete("/v1/contacts/{contact_id}", status_code=204)
def contact_delete(
    contact_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Response:
    _require_permission(ctx, "contact.manage")
    item = _tenant_item(s, Contact, contact_id, ctx["tenant"])
    s.delete(item)
    audit(s, ctx, "contact.deleted")
    s.commit()
    return Response(status_code=204)


@router.get("/v1/lists")
def lists(ctx: dict = Depends(auth), s: Session = Depends(db)) -> dict[str, Any]:
    return {
        "items": jsonable_encoder(
            s.scalars(
                select(ContactList)
                .where(ContactList.tenant_id == ctx["tenant"])
                .order_by(ContactList.created_at.desc())
            ).all()
        )
    }


@router.post("/v1/lists", status_code=201)
def list_create(
    body: ListIn, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    _require_permission(ctx, "contact.manage")
    item = ContactList(
        id=str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        name=body.name,
        description=body.description,
    )
    s.add(item)
    audit(s, ctx, "contact_list.created")
    s.commit()
    s.refresh(item)
    return jsonable_encoder(item)


@router.get("/v1/lists/{list_id}")
def list_detail(
    list_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    return jsonable_encoder(_tenant_item(s, ContactList, list_id, ctx["tenant"]))


@router.patch("/v1/lists/{list_id}")
def list_patch(
    list_id: str, body: ListPatch, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    _require_permission(ctx, "contact.manage")
    item = _tenant_item(s, ContactList, list_id, ctx["tenant"])
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = now()
    audit(s, ctx, "contact_list.updated")
    s.commit()
    s.refresh(item)
    return jsonable_encoder(item)


@router.delete("/v1/lists/{list_id}", status_code=204)
def list_delete(
    list_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Response:
    _require_permission(ctx, "contact.manage")
    item = _tenant_item(s, ContactList, list_id, ctx["tenant"])
    s.delete(item)
    audit(s, ctx, "contact_list.deleted")
    s.commit()
    return Response(status_code=204)


@router.patch("/v1/campaigns/{campaign_id}")
def campaign_patch(
    campaign_id: str,
    body: CampaignPatch,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> Any:
    from .main import Campaign

    _require_permission(ctx, "campaign.manage")
    item = _tenant_item(s, Campaign, campaign_id, ctx["tenant"])
    if item.status not in {"draft", "paused"}:
        raise HTTPException(409, "campaign_not_editable")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    if body.model_dump(exclude_unset=True):
        item.current_version += 1
    audit(s, ctx, "campaign.updated")
    s.commit()
    return item


@router.put("/v1/campaigns/{campaign_id}/dispatch-configuration")
def campaign_dispatch_configuration(
    campaign_id: str,
    body: CampaignDispatchConfiguration,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
) -> dict[str, Any]:
    from .main import Campaign

    _require_permission(ctx, "campaign.manage")
    item = _tenant_item_for_update(s, Campaign, campaign_id, ctx["tenant"])
    if item.status not in {"draft", "paused"}:
        raise HTTPException(409, "campaign_not_editable")
    values = body.model_dump()
    if any(getattr(item, key) != value for key, value in values.items()):
        for key, value in values.items():
            setattr(item, key, value)
        item.current_version += 1
        audit(s, ctx, "campaign.dispatch_configuration.updated")
        s.commit()
    return {
        "id": item.id,
        "sender_id": item.sender_id,
        "template_id": item.template_id,
        "segment_id": item.segment_id,
        "campaign_version": item.current_version,
    }


@router.post(
    "/v1/campaigns/{campaign_id}/schedule",
    status_code=202,
    responses={
        409: {
            "description": "Campaign dispatcher unavailable; scheduling is not accepted."
        }
    },
)
def campaign_schedule(
    campaign_id: str,
    body: CampaignSchedule,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    from .main import Campaign

    _require_permission(ctx, "campaign.manage")
    item = _tenant_item_for_update(s, Campaign, campaign_id, ctx["tenant"])
    if body.scheduled_at.astimezone(timezone.utc) <= now():
        raise HTTPException(422, "schedule_must_be_future")
    from .campaign_dispatcher import (
        CampaignAudienceSnapshot,
        enabled as campaign_dispatcher_enabled,
        schedule_campaign,
    )

    if not campaign_dispatcher_enabled():
        raise HTTPException(409, "campaign_dispatcher_unavailable")
    prior, storage_key, request_hash = _idempotency_begin(
        s,
        ctx,
        idempotency_key,
        action="campaign.schedule",
        resource=campaign_id,
        semantic_payload=body.model_dump(mode="json"),
    )
    if prior is not None:
        return prior
    run = schedule_campaign(s, item, body.scheduled_at.astimezone(timezone.utc))
    audience_count = int(
        s.scalar(
            select(func.count())
            .select_from(CampaignAudienceSnapshot)
            .where(
                CampaignAudienceSnapshot.campaign_id == item.id,
                CampaignAudienceSnapshot.campaign_version == run.campaign_version,
            )
        )
        or 0
    )
    result = {
        "id": item.id,
        "status": item.status,
        "dispatch_run_id": run.id,
        "campaign_version": run.campaign_version,
        "audience_count": audience_count,
        "scheduled_at": run.scheduled_at,
    }
    audit(s, ctx, "campaign.scheduled")
    _idempotency_complete(
        s,
        ctx,
        storage_key=storage_key,
        request_hash=request_hash,
        resource=campaign_id,
        response=jsonable_encoder(result),
    )
    s.commit()
    return result


@router.post("/v1/campaigns/{campaign_id}/pause")
def campaign_pause(
    campaign_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    from .main import Campaign
    from .campaign_dispatcher import pause_campaign

    _require_permission(ctx, "campaign.manage")
    item = _tenant_item_for_update(s, Campaign, campaign_id, ctx["tenant"])
    prior, storage_key, request_hash = _idempotency_begin(
        s,
        ctx,
        idempotency_key,
        action="campaign.pause",
        resource=campaign_id,
        semantic_payload={},
    )
    if prior is not None:
        return prior
    run = pause_campaign(s, item)
    result = {"id": item.id, "status": item.status, "dispatch_run_id": run.id}
    audit(s, ctx, "campaign.paused")
    _idempotency_complete(
        s,
        ctx,
        storage_key=storage_key,
        request_hash=request_hash,
        resource=campaign_id,
        response=result,
    )
    s.commit()
    return result


@router.post("/v1/campaigns/{campaign_id}/resume", status_code=202)
def campaign_resume(
    campaign_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    from .main import Campaign
    from .campaign_dispatcher import resume_campaign

    _require_permission(ctx, "campaign.manage")
    item = _tenant_item_for_update(s, Campaign, campaign_id, ctx["tenant"])
    prior, storage_key, request_hash = _idempotency_begin(
        s,
        ctx,
        idempotency_key,
        action="campaign.resume",
        resource=campaign_id,
        semantic_payload={},
    )
    if prior is not None:
        return prior
    run = resume_campaign(s, item)
    result = {"id": item.id, "status": item.status, "dispatch_run_id": run.id}
    audit(s, ctx, "campaign.resumed")
    _idempotency_complete(
        s,
        ctx,
        storage_key=storage_key,
        request_hash=request_hash,
        resource=campaign_id,
        response=result,
    )
    s.commit()
    return result


@router.post("/v1/campaigns/{campaign_id}/cancel")
def campaign_cancel(
    campaign_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    from .main import Campaign

    _require_permission(ctx, "campaign.manage")
    item = _tenant_item_for_update(s, Campaign, campaign_id, ctx["tenant"])
    prior, storage_key, request_hash = _idempotency_begin(
        s,
        ctx,
        idempotency_key,
        action="campaign.cancel",
        resource=campaign_id,
        semantic_payload={},
    )
    if prior is not None:
        return prior
    if item.status in {"completed", "cancelled"}:
        raise HTTPException(409, "campaign_terminal")
    from .campaign_dispatcher import cancel_campaign

    cancel_campaign(s, item)
    audit(s, ctx, "campaign.cancelled")
    result = {"id": item.id, "status": item.status}
    _idempotency_complete(
        s,
        ctx,
        storage_key=storage_key,
        request_hash=request_hash,
        resource=campaign_id,
        response=result,
    )
    s.commit()
    return result


@router.get("/v1/tracking/events")
def tracking_events(
    ctx: dict = Depends(auth), s: Session = Depends(db), limit: int = 100
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    rows = s.scalars(
        select(Event)
        .where(Event.tenant_id == ctx["tenant"])
        .order_by(Event.created_at.desc())
        .limit(limit)
    ).all()
    return {"items": rows}


@router.get("/v1/tracking/events/{event_id}")
def tracking_event_detail(
    event_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    return _tenant_item(s, Event, event_id, ctx["tenant"])


@router.get("/v1/tracking/messages/{message_id}")
def tracking_message(
    message_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    message = _tenant_item(s, Message, message_id, ctx["tenant"])
    events = s.scalars(
        select(Event)
        .where(Event.tenant_id == ctx["tenant"], Event.message_id == message.id)
        .order_by(Event.created_at)
    ).all()
    return {"message": message, "events": events}


@router.post("/v1/suppressions", status_code=201)
def suppression_create(
    body: SuppressionIn, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Any:
    _require_permission(ctx, "contact.manage")
    email = str(body.email).lower()
    item = s.scalar(
        select(Suppression).where(
            Suppression.tenant_id == ctx["tenant"], Suppression.email == email
        )
    )
    if item is None:
        item = Suppression(
            id=str(uuid.uuid4()),
            tenant_id=ctx["tenant"],
            email=email,
            reason=body.reason,
        )
    item.reason = body.reason
    s.add(item)
    audit(s, ctx, "suppression.upserted")
    s.commit()
    return item


@router.delete("/v1/suppressions/{suppression_id}", status_code=204)
def suppression_delete(
    suppression_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> Response:
    _require_permission(ctx, "contact.manage")
    item = _tenant_item(s, Suppression, suppression_id, ctx["tenant"])
    s.delete(item)
    audit(s, ctx, "suppression.deleted")
    s.commit()
    return Response(status_code=204)


def _outcome_events(kind: str, ctx: dict, s: Session) -> dict[str, Any]:
    rows = s.scalars(
        select(Event)
        .where(
            Event.tenant_id == ctx["tenant"],
            Event.kind.in_([f"email.{kind}", f"klyrow.email.{kind}"]),
        )
        .order_by(Event.created_at.desc())
    ).all()
    return {"items": rows}


@router.get("/v1/bounces")
def bounces(ctx: dict = Depends(auth), s: Session = Depends(db)) -> dict[str, Any]:
    return _outcome_events("bounced", ctx, s)


@router.get("/v1/complaints")
def complaints(ctx: dict = Depends(auth), s: Session = Depends(db)) -> dict[str, Any]:
    return _outcome_events("complained", ctx, s)


@router.get("/v1/billing/account")
def billing_account(
    ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    subscription = s.scalar(
        select(BillingSubscription).where(
            BillingSubscription.tenant_id == ctx["tenant"]
        )
    )
    wallet = s.get(Wallet, ctx["tenant"])
    return {"tenant_id": ctx["tenant"], "subscription": subscription, "wallet": wallet}


@router.get("/v1/billing/plans")
def billing_plans(
    ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    del ctx
    plans = s.scalars(
        select(BillingPlan).where(BillingPlan.active == True).order_by(BillingPlan.name)
    ).all()
    return {
        "items": [
            {
                "plan": plan,
                "prices": s.scalars(
                    select(BillingPrice)
                    .where(
                        BillingPrice.plan_id == plan.id, BillingPrice.retired_at == None
                    )
                    .order_by(BillingPrice.version.desc())
                ).all(),
            }
            for plan in plans
        ]
    }


@router.get("/v1/operations")
def operations(
    ctx: dict = Depends(auth), s: Session = Depends(db), limit: int = 100
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    command_rows = s.scalars(
        select(MiddlewareCommandOperation)
        .where(MiddlewareCommandOperation.tenant_id == ctx["tenant"])
        .order_by(MiddlewareCommandOperation.created_at.desc())
        .limit(limit)
    ).all()
    integration_rows = s.scalars(
        select(IntegrationOutbox)
        .where(IntegrationOutbox.tenant_id == ctx["tenant"])
        .order_by(IntegrationOutbox.created_at.desc())
        .limit(limit)
    ).all()
    visible_rows = [
        item
        for item in [*command_rows, *integration_rows]
        if not isinstance(item, IntegrationOutbox)
        or item.target != "MAUTIC"
        or _has_permission(ctx, _mautic_permission(item.event_type))
    ]
    items = _operations_json(visible_rows, s)
    items.sort(key=lambda value: value["resource_version"], reverse=True)
    return {"items": items[:limit]}


@router.get("/v1/operations/{operation_id}/events")
def operation_events(
    operation_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    item = _find_operation(s, operation_id, ctx["tenant"])
    _authorize_operation_read(ctx, item)
    events: list[dict[str, Any]] = [
        {"status": _operation_json(item, s)["status"], "at": item.updated_at}
    ]
    if isinstance(item, IntegrationOutbox):
        for result in s.scalars(
            select(IntegrationResult)
            .where(
                IntegrationResult.outbox_id == item.id,
                IntegrationResult.tenant_id == ctx["tenant"],
            )
            .order_by(IntegrationResult.created_at)
        ).all():
            events.append(
                {
                    "status": "RECONCILIATION_REQUIRED"
                    if result.source.endswith("_LATE")
                    else "SUCCEEDED",
                    "at": result.created_at,
                    "result_id": result.id,
                }
            )
    return {"operation_id": operation_id, "items": events}


@router.get("/v1/operations/{operation_id}/attempts")
def operation_attempts(
    operation_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    item = _find_operation(s, operation_id, ctx["tenant"])
    _authorize_operation_read(ctx, item)
    attempts = item.attempts if isinstance(item, IntegrationOutbox) else 0
    return {"operation_id": operation_id, "attempts": attempts}


@router.post("/v1/operations/{operation_id}/cancel")
def operation_cancel(
    operation_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    item = _find_operation_for_update(s, operation_id, ctx["tenant"])
    _authorize_operation_mutation(ctx, item)
    prior, storage_key, request_hash = _idempotency_begin(
        s,
        ctx,
        idempotency_key,
        action="operation.cancel",
        resource=operation_id,
        semantic_payload={},
    )
    if prior is not None:
        return prior
    if isinstance(item, MiddlewareCommandOperation):
        if item.state in {"completed", "failed", "cancelled"}:
            raise HTTPException(409, "operation_terminal")
        if item.state == "processing":
            raise HTTPException(409, "operation_processing_not_cancellable")
        item.state = "cancelled"
        item.updated_at = now()
    else:
        if item.state in {"COMPLETED", "DEAD_LETTER", "CANCELLED"}:
            raise HTTPException(409, "operation_terminal")
        if item.state == "PROCESSING":
            raise HTTPException(409, "operation_processing_not_cancellable")
        item.state = "CANCELLED"
        item.updated_at = now()
    audit(s, ctx, "operation.cancelled")
    result = _operation_json(item, s)
    _idempotency_complete(
        s,
        ctx,
        storage_key=storage_key,
        request_hash=request_hash,
        resource=operation_id,
        response=result,
    )
    s.commit()
    return result


@router.post("/v1/operations/{operation_id}/reconcile")
def operation_reconcile(
    operation_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    item = _find_operation_for_update(s, operation_id, ctx["tenant"])
    _authorize_operation_mutation(ctx, item)
    prior, storage_key, request_hash = _idempotency_begin(
        s,
        ctx,
        idempotency_key,
        action="operation.reconcile",
        resource=operation_id,
        semantic_payload={},
    )
    if prior is not None:
        return prior
    changed = False
    if isinstance(item, MiddlewareCommandOperation):
        if item.state == "failed":
            item.state = "accepted"
            item.error = None
            item.updated_at = now()
            changed = True
    else:
        require_safe_integration_recovery(s, item)
        if item.state in {"RETRY", "DEAD_LETTER"}:
            item.state = "PENDING"
            item.last_error = None
            item.next_attempt_at = now()
            item.updated_at = now()
            changed = True
    if changed:
        audit(s, ctx, "operation.reconciliation_requested")
    result = _operation_json(item, s)
    _idempotency_complete(
        s,
        ctx,
        storage_key=storage_key,
        request_hash=request_hash,
        resource=operation_id,
        response=result,
    )
    s.commit()
    return result


@router.get("/v1/providers/postal/health")
def postal_health(
    ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    pending = (
        s.scalar(
            select(func.count())
            .select_from(EmailOutbox)
            .where(
                EmailOutbox.tenant_id == ctx["tenant"],
                EmailOutbox.state.in_(("pending", "retry", "sending")),
            )
        )
        or 0
    )
    failed = (
        s.scalar(
            select(func.count())
            .select_from(EmailOutbox)
            .where(
                EmailOutbox.tenant_id == ctx["tenant"],
                EmailOutbox.state.in_(("failed", "INDETERMINATE")),
            )
        )
        or 0
    )
    configured = bool(
        os.getenv("KLYROW_POSTAL_API_URL")
        and (
            os.getenv("KLYROW_POSTAL_API_KEY_FILE")
            or os.getenv("KLYROW_POSTAL_API_KEY")
        )
    )
    return {
        "provider": "postal",
        "status": "ok" if configured and failed == 0 else "degraded",
        "configured": configured,
        "queue_active": pending,
        "queue_failed": failed,
    }


@router.get("/v1/providers/postal/status")
def postal_status(
    ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    return {
        **postal_health(ctx, s),
        "timeout_seconds": 10,
        "maximum_attempts": 5,
        "idempotency": "durable",
        "ambiguous_state": "INDETERMINATE",
        "reconciliation": True,
    }


@router.post("/v1/integrations/mautic/commands", status_code=202)
def mautic_command(
    body: MauticCommand,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
    x_correlation_id: str = Header(
        alias="X-Correlation-ID", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    _require_mautic_permission(ctx, body.command)
    from .mautic_adapter import mautic_request

    try:
        mautic_request(body.command, body.payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, "mautic_command_payload_invalid") from error
    action = "mautic.command"
    semantic_payload = {
        "command": body.command,
        "aggregate_id": body.aggregate_id,
        "payload": body.payload,
    }
    digest = semantic_request_hash(
        action=action, resource=body.aggregate_id, payload=semantic_payload
    )
    storage_key = scoped_idempotency_key(
        ctx, idempotency_key, action=action, resource=body.aggregate_id
    )
    prior = s.scalar(
        select(IntegrationOutbox).where(
            IntegrationOutbox.tenant_id == ctx["tenant"],
            IntegrationOutbox.target == "MAUTIC",
            IntegrationOutbox.idempotency_key == storage_key,
        )
    )
    if prior is None:
        legacy = s.scalar(
            select(IntegrationOutbox).where(
                IntegrationOutbox.tenant_id == ctx["tenant"],
                IntegrationOutbox.target == "MAUTIC",
                IntegrationOutbox.idempotency_key == idempotency_key,
            )
        )
        if legacy:
            legacy_semantic = json.dumps(
                {**semantic_payload, "tenant_id": ctx["tenant"]},
                separators=(",", ":"),
                sort_keys=True,
            )
            try:
                legacy_digest = json.loads(legacy.payload_json).get("semantic_sha256")
            except (TypeError, ValueError):
                legacy_digest = None
            if legacy_digest != hashlib.sha256(legacy_semantic.encode()).hexdigest():
                raise HTTPException(409, "idempotency_key_payload_mismatch")
            return _operation_json(legacy, s)
    if prior:
        prior_payload = json.loads(prior.payload_json)
        if prior_payload.get("semantic_sha256") != digest:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        return _operation_json(prior, s)
    payload = {
        "envelope": {
            "request_id": body.request_id,
            "correlation_id": x_correlation_id,
            "tenant_id": ctx["tenant"],
            "actor": ctx["sub"],
            "operation_id": body.operation_id,
            "idempotency_key": idempotency_key,
            "api_version": "v1",
            "timestamp": body.timestamp.isoformat(),
            "trace_context": body.trace_context,
        },
        "command": body.command,
        "payload": body.payload,
        "semantic_sha256": digest,
    }
    item = IntegrationOutbox(
        id=body.operation_id or str(uuid.uuid4()),
        tenant_id=ctx["tenant"],
        target="MAUTIC",
        event_type=body.command,
        aggregate_id=body.aggregate_id,
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        idempotency_key=storage_key,
    )
    s.add(item)
    audit(s, ctx, "mautic.command.queued")
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        prior = s.scalar(
            select(IntegrationOutbox).where(
                IntegrationOutbox.tenant_id == ctx["tenant"],
                IntegrationOutbox.target == "MAUTIC",
                IntegrationOutbox.idempotency_key == storage_key,
            )
        )
        if prior is None:
            raise
        try:
            prior_digest = json.loads(prior.payload_json).get("semantic_sha256")
        except (TypeError, ValueError):
            prior_digest = None
        if prior_digest != digest:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        return _operation_json(prior, s)
    return _operation_json(item, s)


@router.get("/v1/integrations/mautic/operations")
def mautic_operations(
    ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    rows = s.scalars(
        select(IntegrationOutbox)
        .where(
            IntegrationOutbox.tenant_id == ctx["tenant"],
            IntegrationOutbox.target == "MAUTIC",
        )
        .order_by(IntegrationOutbox.created_at.desc())
        .limit(200)
    ).all()
    visible = [
        item
        for item in rows
        if _has_permission(ctx, _mautic_permission(item.event_type))
    ]
    return {"items": _operations_json(visible, s)}


@router.get("/v1/integrations/mautic/operations/{operation_id}")
def mautic_operation(
    operation_id: str, ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    item = _tenant_item(s, IntegrationOutbox, operation_id, ctx["tenant"])
    if item.target != "MAUTIC":
        raise HTTPException(404, "not_found")
    _authorize_operation_read(ctx, item)
    return _operation_json(item, s)


@router.post("/v1/integrations/mautic/operations/{operation_id}/reconcile")
def mautic_reconcile(
    operation_id: str,
    ctx: dict = Depends(auth),
    s: Session = Depends(db),
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=8, max_length=200
    ),
) -> dict[str, Any]:
    item = _tenant_item(s, IntegrationOutbox, operation_id, ctx["tenant"])
    if item.target != "MAUTIC":
        raise HTTPException(404, "not_found")
    return operation_reconcile(operation_id, ctx, s, idempotency_key)


@router.get("/v1/system/capabilities")
def system_capabilities(ctx: dict = Depends(auth)) -> dict[str, Any]:
    del ctx
    return {
        "api_version": "v1",
        "capabilities": [
            "identity",
            "tenancy",
            "email",
            "postal",
            "mautic",
            "billing",
            "operations",
            "tracking",
        ],
    }


@router.get("/v1/system/readiness")
def system_readiness(
    ctx: dict = Depends(auth), s: Session = Depends(db)
) -> dict[str, Any]:
    s.execute(text("SELECT 1"))
    postal = postal_health(ctx, s)
    return {
        "status": "ready" if postal["configured"] else "degraded",
        "database": "ok",
        "postal": postal["status"],
        "source_sha": os.getenv("KLYROW_SOURCE_SHA", "development"),
    }
