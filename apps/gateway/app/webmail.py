"""Tenant-isolated browser webmail built on the governed Klyrow mail engine.

Klyrow identities authenticate through the existing OIDC BFF.  A mailbox is a
view over one verified sender address; it never owns a second password.  Owners
and administrators can access every mailbox in their tenant, while other team
members require an explicit mailbox grant in addition to their workspace role.
"""
from __future__ import annotations

import html
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from .auth_bff import BrowserSession, browser_context, csrf_guard
from .main import AllowedSender, Domain, EmailOutbox, InboundRouteConfig, MailIn, _send, audit, db
from .tenancy import ROLE_PERMISSIONS
from .webmail_models import WebmailAccess, WebmailAttachment, WebmailMailbox, WebmailMessage


router = APIRouter(prefix="/app/api/mailboxes", tags=["Browser webmail"])
now = lambda: datetime.now(timezone.utc)
FOLDERS = {"INBOX", "SENT", "DRAFTS", "STARRED", "ARCHIVE", "SPAM", "TRASH"}
STORED_FOLDERS = FOLDERS - {"STARRED"}
MANAGER_ROLES = {"OWNER", "ADMIN", "platform_admin", "tenant_admin"}
INBOUND_LOCAL_PARTS = {"appolon", "billing", "support"}


class DraftIn(BaseModel):
    to: Optional[EmailStr] = None
    subject: str = Field(default="", max_length=998)
    text: str = Field(default="", max_length=1_000_000)
    reply_to_message_id: Optional[str] = None


class ComposeIn(BaseModel):
    to: EmailStr
    subject: str = Field(default="", max_length=998)
    text: str = Field(min_length=1, max_length=1_000_000)
    draft_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None


class MessageUpdate(BaseModel):
    folder: Optional[str] = None
    is_read: Optional[bool] = None
    is_starred: Optional[bool] = None

    @model_validator(mode="after")
    def validate_folder(self):
        if self.folder is not None:
            self.folder = self.folder.upper()
            if self.folder not in STORED_FOLDERS:
                raise ValueError("invalid_mailbox_folder")
        return self


class AccessGrantIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(OWNER|SENDER|READER)$")


def _json_list(value: Optional[str]) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _addresses(value: Optional[str]) -> list[str]:
    return sorted({address.lower() for _, address in getaddresses([value or ""]) if address})


def _preview(value: Optional[str], limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _workspace_permission(ctx: dict, permission: str) -> bool:
    if ctx.get("role") in MANAGER_ROLES:
        return True
    allowed = ROLE_PERMISSIONS.get(str(ctx.get("role", "")).upper(), set())
    return "*" in allowed or permission in allowed


def _manager(ctx: dict) -> None:
    if ctx.get("role") not in MANAGER_ROLES:
        raise HTTPException(403, "tenant_management_denied")


def _mailbox(s: Session, ctx: dict, mailbox_id: str, permission: str = "mail.read") -> WebmailMailbox:
    item = s.scalar(select(WebmailMailbox).where(
        WebmailMailbox.id == mailbox_id,
        WebmailMailbox.tenant_id == ctx["tenant"],
        WebmailMailbox.status == "ACTIVE",
    ))
    workspace_permission = "mail.read" if permission == "mail.manage" else permission
    if not item or not _workspace_permission(ctx, workspace_permission):
        raise HTTPException(404, "mailbox_not_found")
    if ctx.get("role") in MANAGER_ROLES:
        return item
    grant = s.scalar(select(WebmailAccess).where(
        WebmailAccess.tenant_id == ctx["tenant"],
        WebmailAccess.mailbox_id == item.id,
        WebmailAccess.user_id == ctx["sub"],
    ))
    if not grant or (permission == "mail.send" and grant.role not in {"OWNER", "SENDER"}) or (
        permission == "mail.manage" and grant.role != "OWNER"
    ):
        raise HTTPException(404, "mailbox_not_found")
    return item


def _message(s: Session, mailbox: WebmailMailbox, message_id: str) -> WebmailMessage:
    item = s.scalar(select(WebmailMessage).where(
        WebmailMessage.id == message_id,
        WebmailMessage.tenant_id == mailbox.tenant_id,
        WebmailMessage.mailbox_id == mailbox.id,
        WebmailMessage.deleted_at.is_(None),
    ))
    if not item:
        raise HTTPException(404, "message_not_found")
    return item


def _message_header(item_id: str, domain: str) -> str:
    return f"<{item_id}@webmail.{domain}>"


def _thread_for_headers(s: Session, mailbox_id: str, in_reply_to: Optional[str], references: list[str]) -> str:
    candidates = [value for value in [in_reply_to, *references] if value]
    if candidates:
        parent = s.scalar(select(WebmailMessage).where(
            WebmailMessage.mailbox_id == mailbox_id,
            WebmailMessage.message_id_header.in_(candidates),
        ).order_by(WebmailMessage.created_at.desc()))
        if parent:
            return parent.thread_id
    return str(uuid.uuid4())


def _summary(item: WebmailMessage) -> dict:
    return {
        "id": item.id,
        "thread_id": item.thread_id,
        "direction": item.direction,
        "folder": item.folder,
        "from": item.from_address,
        "to": _json_list(item.to_json),
        "subject": item.subject,
        "preview": _preview(item.text_body),
        "is_read": item.is_read,
        "is_starred": item.is_starred,
        "delivery_status": item.delivery_status,
        "has_attachments": bool(_json_list(item.attachments_json)),
        "occurred_at": item.received_at or item.sent_at or item.updated_at,
    }


def _detail(item: WebmailMessage) -> dict:
    return {
        **_summary(item),
        "cc": _json_list(item.cc_json),
        "bcc": _json_list(item.bcc_json),
        "reply_to": item.reply_to,
        "text": item.text_body or "",
        # Untrusted HTML is returned for clients that have their own sanitizer.
        # The bundled UI deliberately renders only the escaped plain-text body.
        "html": item.html_body,
        "attachments": _json_list(item.attachments_json),
        "message_id": item.message_id_header,
        "in_reply_to": item.in_reply_to,
        "reply_to_message_id": item.reply_to_message_id,
        "references": _json_list(item.references_json),
    }


def sync_verified_mailboxes(s: Session, tenant_id: Optional[str] = None) -> dict:
    """Materialize every enabled sender on a verified domain as a webmail mailbox.

    Existing inbound route ownership is preserved.  A mailbox is receive-ready
    only when its exact route and provider domain are already enabled.
    """
    from .provider import ProviderDomain

    # Gateway and billing-api both run the startup reconciliation. Serialize
    # the transaction on PostgreSQL so only one process can materialize a
    # previously absent (tenant, address) mailbox at a time.
    if s.get_bind().dialect.name == "postgresql":
        s.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 49367291020260902})

    domains_query = select(Domain).where(Domain.verified == True)
    if tenant_id:
        domains_query = domains_query.where(Domain.tenant_id == tenant_id)
    created = 0
    updated = 0
    send_ready = 0
    pending_send = 0
    receive_ready = 0
    pending_receive = 0
    for domain in s.scalars(domains_query.order_by(Domain.domain)).all():
        provider_domain = s.scalar(select(ProviderDomain).where(
            ProviderDomain.tenant_id == domain.tenant_id,
            ProviderDomain.domain == domain.domain.lower(),
        ))
        provider_send = bool(provider_domain and provider_domain.sending_enabled and provider_domain.status == "SENDING_ENABLED")
        provider_receive = bool(provider_domain and provider_domain.inbound_enabled and provider_domain.status in {"VERIFIED", "SENDING_ENABLED"})
        senders = s.scalars(select(AllowedSender).where(
            AllowedSender.tenant_id == domain.tenant_id,
            AllowedSender.enabled == True,
        ).order_by(AllowedSender.address)).all()
        for sender in senders:
            address = sender.address.lower()
            if address.rsplit("@", 1)[-1] != domain.domain.lower():
                continue
            route = s.scalar(select(InboundRouteConfig).where(
                InboundRouteConfig.tenant_id == domain.tenant_id,
                InboundRouteConfig.address == address,
            ))
            receiving = bool(
                provider_receive
                and route
                and route.verified
                and route.enabled
                and route.destination_kind == "webmail"
                and route.destination_ref == "klyrow:webmail"
            )
            mailbox = s.scalar(select(WebmailMailbox).where(
                WebmailMailbox.tenant_id == domain.tenant_id,
                WebmailMailbox.address == address,
            ))
            if mailbox is None:
                mailbox = WebmailMailbox(
                    id=str(uuid.uuid4()), tenant_id=domain.tenant_id, domain_id=domain.id,
                    address=address, display_name=address.split("@", 1)[0].replace(".", " ").replace("-", " ").title(),
                    sending_enabled=provider_send, receiving_enabled=receiving,
                )
                s.add(mailbox)
                created += 1
            else:
                mailbox.domain_id = domain.id
                mailbox.sending_enabled = provider_send
                mailbox.receiving_enabled = receiving
                mailbox.status = "ACTIVE"
                mailbox.updated_at = now()
                updated += 1
            send_ready += int(provider_send)
            pending_send += int(not provider_send)
            receive_ready += int(receiving)
            pending_receive += int(not receiving)
    s.commit()
    return {
        "created": created, "updated": updated,
        "send_ready": send_ready, "pending_send": pending_send,
        "receive_ready": receive_ready, "pending_receive": pending_receive,
    }


async def _reconcile_postal_inbound(tenant_id: str, addresses: list[str]) -> dict:
    from .postal_provisioning import _bridge_token

    base = os.getenv("KLYROW_POSTAL_PROVISIONER_URL", "http://postal-provisioner:9090").rstrip("/")
    headers = {"Authorization": "Bearer " + _bridge_token(), "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
            response = await client.post(
                base + "/v1/reconcile-inbound", headers=headers,
                json={"tenant_id": tenant_id, "addresses": addresses},
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(503, "postal_inbound_reconciliation_failed") from exc
    returned = sorted(str(item).lower() for item in result.get("addresses", []))
    if returned != sorted(addresses):
        raise HTTPException(502, "postal_inbound_reconciliation_incomplete")
    return result


async def _reconcile_postal_outbound(s: Session, tenant_id: str, domains: list[str]) -> dict:
    from .postal_provisioning import reconcile_live_domain_credentials

    try:
        return await reconcile_live_domain_credentials(s, tenant_id, domains)
    except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(503, "postal_outbound_reconciliation_failed") from exc


def capture_provider_inbound(s: Session, route: InboundRouteConfig, provider_item, parsed: dict) -> Optional[WebmailMessage]:
    """Copy one authenticated provider message into its matching mailbox."""
    if provider_item.disposition == "REJECT":
        return None
    if route.destination_kind != "webmail" or route.destination_ref != "klyrow:webmail":
        return None
    existing = s.scalar(select(WebmailMessage).where(WebmailMessage.provider_inbound_id == provider_item.id))
    if existing:
        return existing
    mailbox = s.scalar(select(WebmailMailbox).where(
        WebmailMailbox.tenant_id == provider_item.tenant_id,
        WebmailMailbox.address == provider_item.recipient.lower(),
        WebmailMailbox.status == "ACTIVE",
    ).with_for_update())
    if not mailbox:
        return None
    references = re.findall(r"<[^>]+>", parsed.get("references") or "")
    item_id = str(uuid.uuid4())
    attachment_metadata = parsed.get("attachments") or []
    attachment_contents = parsed.get("attachment_contents") or []
    if len(attachment_metadata) != len(attachment_contents):
        raise HTTPException(500, "inbound_attachment_content_mismatch")
    stored_bytes = sum(len(str(parsed.get(field) or "").encode("utf-8")) for field in ("subject", "text", "html"))
    stored_bytes += sum(len(content) for content in attachment_contents if isinstance(content, bytes))
    if mailbox.storage_used_bytes + stored_bytes > mailbox.storage_quota_bytes:
        raise HTTPException(507, "mailbox_storage_quota_exceeded")
    stored_attachments = []
    public_attachments = []
    for metadata, content in zip(attachment_metadata, attachment_contents, strict=True):
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != metadata.get("sha256"):
            raise HTTPException(422, "inbound_attachment_digest_mismatch")
        attachment_id = str(uuid.uuid4())
        public_attachments.append({
            **metadata,
            "id": attachment_id,
            "download_url": f"/app/api/mailboxes/{mailbox.id}/messages/{item_id}/attachments/{attachment_id}",
        })
        stored_attachments.append(WebmailAttachment(
            id=attachment_id,
            tenant_id=mailbox.tenant_id,
            mailbox_id=mailbox.id,
            message_id=item_id,
            filename=metadata["filename"],
            content_type=metadata["content_type"],
            size=metadata["size"],
            sha256=metadata["sha256"],
            content=content,
        ))
    item = WebmailMessage(
        id=item_id,
        tenant_id=mailbox.tenant_id,
        mailbox_id=mailbox.id,
        thread_id=_thread_for_headers(s, mailbox.id, parsed.get("in_reply_to"), references),
        provider_inbound_id=provider_item.id,
        direction="INBOUND",
        folder="SPAM" if provider_item.disposition == "QUARANTINE" else "INBOX",
        message_id_header=parsed.get("message_id") or _message_header(item_id, mailbox.address.rsplit("@", 1)[-1]),
        in_reply_to=parsed.get("in_reply_to"),
        references_json=json.dumps(references),
        from_address=(_addresses(parsed.get("from")) or [parsed.get("from") or "unknown"])[0],
        to_json=json.dumps(_addresses(parsed.get("to")) or [mailbox.address]),
        cc_json=json.dumps(_addresses(parsed.get("cc"))),
        subject=parsed.get("subject") or "(no subject)",
        text_body=parsed.get("text") or "",
        html_body=parsed.get("html"),
        attachments_json=json.dumps(public_attachments, separators=(",", ":"), sort_keys=True),
        is_read=False,
        delivery_status="RECEIVED",
        received_at=now(),
    )
    mailbox.receiving_enabled = True
    mailbox.storage_used_bytes += stored_bytes
    mailbox.updated_at = now()
    s.add(item)
    s.add_all(stored_attachments)
    return item


def update_outbound_status(s: Session, tenant_id: str, outbound_message_id: str, status: str) -> None:
    item = s.scalar(select(WebmailMessage).where(
        WebmailMessage.tenant_id == tenant_id,
        WebmailMessage.outbound_message_id == outbound_message_id,
    ))
    if item is None:
        # Postal callbacks identify the provider message, while webmail stores
        # the governed local message ID. Resolve the durable outbox mapping.
        outbox = s.scalar(select(EmailOutbox).where(
            EmailOutbox.tenant_id == tenant_id,
            EmailOutbox.provider_message_id == outbound_message_id,
        ))
        if outbox:
            item = s.scalar(select(WebmailMessage).where(
                WebmailMessage.tenant_id == tenant_id,
                WebmailMessage.outbound_message_id == outbox.message_id,
            ))
    if item:
        item.delivery_status = status.upper()
        item.updated_at = now()


@router.get("")
def list_mailboxes(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    if not _workspace_permission(ctx, "mail.read"):
        raise HTTPException(403, "mail_read_denied")
    query = select(WebmailMailbox).where(WebmailMailbox.tenant_id == ctx["tenant"], WebmailMailbox.status == "ACTIVE")
    if ctx.get("role") not in MANAGER_ROLES:
        mailbox_ids = select(WebmailAccess.mailbox_id).where(
            WebmailAccess.tenant_id == ctx["tenant"], WebmailAccess.user_id == ctx["sub"])
        query = query.where(WebmailMailbox.id.in_(mailbox_ids))
    rows = s.scalars(query.order_by(WebmailMailbox.address)).all()
    result = []
    for item in rows:
        counts = {folder: s.scalar(select(func.count()).select_from(WebmailMessage).where(
            WebmailMessage.mailbox_id == item.id,
            WebmailMessage.folder == folder,
            WebmailMessage.deleted_at.is_(None),
        )) or 0 for folder in ("INBOX", "SENT", "DRAFTS", "ARCHIVE", "SPAM", "TRASH")}
        counts["UNREAD"] = s.scalar(select(func.count()).select_from(WebmailMessage).where(
            WebmailMessage.mailbox_id == item.id,
            WebmailMessage.folder == "INBOX",
            WebmailMessage.is_read == False,
            WebmailMessage.deleted_at.is_(None),
        )) or 0
        counts["STARRED"] = s.scalar(select(func.count()).select_from(WebmailMessage).where(
            WebmailMessage.mailbox_id == item.id,
            WebmailMessage.is_starred == True,
            WebmailMessage.deleted_at.is_(None),
        )) or 0
        grants = s.scalars(select(WebmailAccess).where(
            WebmailAccess.tenant_id == ctx["tenant"],
            WebmailAccess.mailbox_id == item.id,
        ).order_by(WebmailAccess.created_at)).all()
        my_access_role = ctx.get("role") if ctx.get("role") in MANAGER_ROLES else next(
            (grant.role for grant in grants if grant.user_id == ctx.get("sub")),
            None,
        )
        result.append({
            "id": item.id, "address": item.address, "domain": item.address.rsplit("@", 1)[-1],
            "display_name": item.display_name, "sending_enabled": item.sending_enabled,
            "receiving_enabled": item.receiving_enabled, "counts": counts,
            "is_shared": bool(grants), "grant_count": len(grants),
            "my_access_role": my_access_role, "unread_count": counts["UNREAD"],
        })
    return result


@router.post("/sync")
def sync_mailboxes(ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _manager(ctx)
    result = sync_verified_mailboxes(s, ctx["tenant"])
    audit(s, ctx, "webmail.mailboxes.synced")
    s.commit()
    return result


@router.post("/inbound/activate")
async def activate_inbound_mailboxes(
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
):
    """Reconcile Postal routes before enabling exact Klyrow inbox routes."""
    from .provider import ProviderDomain

    _manager(ctx)
    candidate_domains = list(s.scalars(select(Domain.domain).join(
        ProviderDomain,
        (ProviderDomain.tenant_id == Domain.tenant_id) & (ProviderDomain.domain == Domain.domain),
    ).where(
        Domain.tenant_id == ctx["tenant"], Domain.verified == True,
        ProviderDomain.status == "SENDING_ENABLED", ProviderDomain.sending_enabled == True,
    ).order_by(Domain.domain)).all())
    if not candidate_domains:
        raise HTTPException(409, "no_verified_sending_domains")
    candidate_domain_set = set(candidate_domains)
    addresses = []
    for address in s.scalars(select(AllowedSender.address).where(
        AllowedSender.tenant_id == ctx["tenant"], AllowedSender.enabled == True,
    ).order_by(AllowedSender.address)).all():
        normalized = address.lower()
        local_part, separator, domain = normalized.partition("@")
        if not separator or domain not in candidate_domain_set or local_part not in INBOUND_LOCAL_PARTS:
            continue
        route = s.scalar(select(InboundRouteConfig).where(
            InboundRouteConfig.tenant_id == ctx["tenant"], InboundRouteConfig.address == normalized,
        ))
        if route and route.destination_ref and (
            route.destination_kind != "webmail" or route.destination_ref != "klyrow:webmail"
        ):
            continue
        addresses.append(normalized)
    addresses = sorted(set(addresses))
    if not addresses:
        raise HTTPException(409, "no_authorized_inbound_addresses")
    await _reconcile_postal_inbound(ctx["tenant"], addresses)
    domains = sorted({address.rsplit("@", 1)[-1] for address in addresses})
    outbound = await _reconcile_postal_outbound(s, ctx["tenant"], domains)

    domain_set = set(domains)
    provider_domains = s.scalars(select(ProviderDomain).where(
        ProviderDomain.tenant_id == ctx["tenant"], ProviderDomain.domain.in_(domain_set),
    )).all()
    for item in provider_domains:
        item.inbound_enabled = True
    activated_routes = 0
    for address in addresses:
        normalized = address.lower()
        local_part, separator, domain = normalized.partition("@")
        if not separator or domain not in domain_set or local_part not in INBOUND_LOCAL_PARTS:
            continue
        route = s.scalar(select(InboundRouteConfig).where(
            InboundRouteConfig.tenant_id == ctx["tenant"], InboundRouteConfig.address == normalized,
        ))
        if route is None:
            route = InboundRouteConfig(
                id=str(uuid.uuid4()), tenant_id=ctx["tenant"], address=normalized,
                destination_kind="webmail", destination_ref="klyrow:webmail",
            )
            s.add(route)
        elif not route.destination_ref:
            route.destination_kind = "webmail"
            route.destination_ref = "klyrow:webmail"
        elif route.destination_kind != "webmail" or route.destination_ref != "klyrow:webmail":
            continue
        route.verified = True
        route.enabled = True
        activated_routes += 1
    audit(s, ctx, "webmail.inbound.activated")
    s.commit()
    synced = sync_verified_mailboxes(s, ctx["tenant"])
    return {
        "activated_domains": len(domains), "activated_routes": activated_routes,
        "domains": domains, "mailboxes": synced,
        "outbound_credentials": len(outbound["domains"]),
    }


@router.get("/{mailbox_id}/messages")
def list_messages(
    mailbox_id: str,
    folder: str = Query(default="INBOX"),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: dict = Depends(browser_context),
    s: Session = Depends(db),
):
    mailbox = _mailbox(s, ctx, mailbox_id)
    folder = folder.upper()
    if folder not in FOLDERS:
        raise HTTPException(422, "invalid_mailbox_folder")
    query = select(WebmailMessage).where(
        WebmailMessage.tenant_id == ctx["tenant"],
        WebmailMessage.mailbox_id == mailbox.id,
        WebmailMessage.deleted_at.is_(None),
    )
    query = query.where(WebmailMessage.is_starred == True) if folder == "STARRED" else query.where(WebmailMessage.folder == folder)
    if q.strip():
        pattern = "%" + q.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(
            WebmailMessage.subject.ilike(pattern, escape="\\"),
            WebmailMessage.from_address.ilike(pattern, escape="\\"),
            WebmailMessage.text_body.ilike(pattern, escape="\\"),
        ))
    rows = s.scalars(query.order_by(func.coalesce(WebmailMessage.received_at, WebmailMessage.sent_at, WebmailMessage.updated_at).desc()).offset(offset).limit(limit)).all()
    return {"items": [_summary(item) for item in rows], "limit": limit, "offset": offset}


@router.get("/{mailbox_id}/messages/{message_id}")
def get_message(mailbox_id: str, message_id: str, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    mailbox = _mailbox(s, ctx, mailbox_id)
    return _detail(_message(s, mailbox, message_id))


@router.get("/{mailbox_id}/messages/{message_id}/attachments/{attachment_id}")
def get_attachment(
    mailbox_id: str,
    message_id: str,
    attachment_id: str,
    ctx: dict = Depends(browser_context),
    s: Session = Depends(db),
):
    mailbox = _mailbox(s, ctx, mailbox_id)
    message = _message(s, mailbox, message_id)
    item = s.scalar(select(WebmailAttachment).where(
        WebmailAttachment.id == attachment_id,
        WebmailAttachment.tenant_id == ctx["tenant"],
        WebmailAttachment.mailbox_id == mailbox.id,
        WebmailAttachment.message_id == message.id,
    ))
    if not item:
        raise HTTPException(404, "attachment_not_found")
    return Response(
        content=item.content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(item.filename, safe=""),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{mailbox_id}/drafts", status_code=201)
def create_draft(mailbox_id: str, payload: DraftIn, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    mailbox = _mailbox(s, ctx, mailbox_id, "mail.send")
    parent = _message(s, mailbox, payload.reply_to_message_id) if payload.reply_to_message_id else None
    item_id = str(uuid.uuid4())
    item = WebmailMessage(
        id=item_id, tenant_id=ctx["tenant"], mailbox_id=mailbox.id,
        thread_id=parent.thread_id if parent else str(uuid.uuid4()), direction="DRAFT", folder="DRAFTS",
        message_id_header=_message_header(item_id, mailbox.address.rsplit("@", 1)[-1]),
        in_reply_to=parent.message_id_header if parent else None,
        reply_to_message_id=parent.id if parent else None,
        references_json=json.dumps([*(_json_list(parent.references_json) if parent else []), parent.message_id_header] if parent and parent.message_id_header else []),
        from_address=mailbox.address, to_json=json.dumps([str(payload.to).lower()] if payload.to else []),
        subject=payload.subject, text_body=payload.text, is_read=True,
    )
    s.add(item)
    audit(s, ctx, "webmail.draft.created")
    s.commit()
    return _detail(item)


@router.put("/{mailbox_id}/drafts/{message_id}")
def update_draft(mailbox_id: str, message_id: str, payload: DraftIn, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    mailbox = _mailbox(s, ctx, mailbox_id, "mail.send")
    item = _message(s, mailbox, message_id)
    if item.folder != "DRAFTS" or item.direction != "DRAFT":
        raise HTTPException(409, "message_is_not_draft")
    item.to_json = json.dumps([str(payload.to).lower()] if payload.to else [])
    item.subject = payload.subject
    item.text_body = payload.text
    item.updated_at = now()
    s.commit()
    return _detail(item)


@router.post("/{mailbox_id}/send", status_code=202)
async def send_message(
    mailbox_id: str,
    payload: ComposeIn,
    ctx: dict = Depends(browser_context),
    _session: BrowserSession = Depends(csrf_guard),
    s: Session = Depends(db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    mailbox = _mailbox(s, ctx, mailbox_id, "mail.send")
    if not mailbox.sending_enabled:
        raise HTTPException(409, "mailbox_sending_disabled")
    if not idempotency_key:
        raise HTTPException(400, "idempotency_key_required")
    parent = _message(s, mailbox, payload.reply_to_message_id) if payload.reply_to_message_id else None
    # The RFC Message-ID is part of the governed outbound payload, so it must be
    # stable across client retries using the same tenant-scoped idempotency key.
    item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"klyrow:webmail:{ctx['tenant']}:{mailbox.id}:{idempotency_key}"))
    message_header = _message_header(item_id, mailbox.address.rsplit("@", 1)[-1])
    references = [*(_json_list(parent.references_json) if parent else [])]
    if parent and parent.message_id_header and parent.message_id_header not in references:
        references.append(parent.message_id_header)
    headers = {"Message-ID": message_header}
    if parent and parent.message_id_header:
        headers["In-Reply-To"] = parent.message_id_header
        headers["References"] = " ".join(references)
    escaped = html.escape(payload.text).replace("\n", "<br>")
    # Mark the server-owned browser path explicitly. Campaign-required mode
    # still protects the general sending API, while authenticated mailbox mail
    # is governed by mailbox access plus the exact sender/domain checks below.
    send_ctx = {**ctx, "_klyrow_mail_channel": "webmail"}
    result = await _send(MailIn(
        to=payload.to, sender=mailbox.address, subject=payload.subject or "(no subject)",
        text=payload.text, html=f"<p>{escaped}</p>", stream="transactional", headers=headers,
    ), send_ctx, s, idempotency_key)
    existing = s.scalar(select(WebmailMessage).where(WebmailMessage.outbound_message_id == result["id"]))
    if existing:
        return {"message": _detail(existing), "delivery": result}
    item = WebmailMessage(
        id=item_id, tenant_id=ctx["tenant"], mailbox_id=mailbox.id,
        thread_id=parent.thread_id if parent else str(uuid.uuid4()), outbound_message_id=result["id"],
        direction="OUTBOUND", folder="SENT", message_id_header=message_header,
        in_reply_to=parent.message_id_header if parent else None, reply_to_message_id=parent.id if parent else None,
        references_json=json.dumps(references),
        from_address=mailbox.address, to_json=json.dumps([str(payload.to).lower()]),
        subject=payload.subject or "(no subject)", text_body=payload.text,
        html_body=f"<p>{escaped}</p>", is_read=True, delivery_status=result["status"].upper(), sent_at=now(),
    )
    s.add(item)
    if payload.draft_id:
        draft = s.scalar(select(WebmailMessage).where(
            WebmailMessage.id == payload.draft_id,
            WebmailMessage.mailbox_id == mailbox.id,
            WebmailMessage.folder == "DRAFTS",
        ))
        if draft:
            draft.deleted_at = now()
    audit(s, ctx, "webmail.message.sent")
    s.commit()
    return {"message": _detail(item), "delivery": result}


@router.patch("/{mailbox_id}/messages/{message_id}")
def update_message(mailbox_id: str, message_id: str, payload: MessageUpdate, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    mailbox = _mailbox(s, ctx, mailbox_id, "mail.manage" if payload.folder is not None else "mail.read")
    item = _message(s, mailbox, message_id)
    if payload.folder is not None:
        item.folder = payload.folder
    if payload.is_read is not None:
        item.is_read = payload.is_read
    if payload.is_starred is not None:
        item.is_starred = payload.is_starred
    item.updated_at = now()
    s.commit()
    return _detail(item)


@router.delete("/{mailbox_id}/messages/{message_id}", status_code=204)
def delete_message(mailbox_id: str, message_id: str, permanent: bool = False, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    mailbox = _mailbox(s, ctx, mailbox_id, "mail.manage")
    item = _message(s, mailbox, message_id)
    if permanent:
        if item.folder != "TRASH":
            raise HTTPException(409, "move_message_to_trash_first")
        attachments = s.scalars(select(WebmailAttachment).where(
            WebmailAttachment.tenant_id == ctx["tenant"],
            WebmailAttachment.mailbox_id == mailbox.id,
            WebmailAttachment.message_id == item.id,
        )).all()
        reclaimed = 0
        if item.direction == "INBOUND":
            reclaimed = sum(len(str(value or "").encode("utf-8")) for value in (item.subject, item.text_body, item.html_body))
            reclaimed += sum(attachment.size for attachment in attachments)
        for attachment in attachments:
            s.delete(attachment)
        item.subject = "(deleted)"
        item.text_body = None
        item.html_body = None
        item.attachments_json = "[]"
        item.deleted_at = now()
        mailbox.storage_used_bytes = max(0, mailbox.storage_used_bytes - reclaimed)
        mailbox.updated_at = now()
    else:
        item.folder = "TRASH"
        item.updated_at = now()
    s.commit()


@router.get("/{mailbox_id}/access")
def list_access(mailbox_id: str, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    _manager(ctx)
    mailbox = _mailbox(s, ctx, mailbox_id)
    rows = s.scalars(select(WebmailAccess).where(WebmailAccess.mailbox_id == mailbox.id).order_by(WebmailAccess.created_at)).all()
    return [{"id": item.id, "user_id": item.user_id, "role": item.role, "created_at": item.created_at} for item in rows]


@router.post("/{mailbox_id}/access", status_code=201)
def grant_access(mailbox_id: str, payload: AccessGrantIn, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _manager(ctx)
    mailbox = _mailbox(s, ctx, mailbox_id)
    item = s.scalar(select(WebmailAccess).where(WebmailAccess.mailbox_id == mailbox.id, WebmailAccess.user_id == payload.user_id))
    if item is None:
        item = WebmailAccess(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], mailbox_id=mailbox.id, user_id=payload.user_id, role=payload.role, created_by=ctx["sub"])
        s.add(item)
    else:
        item.role = payload.role
    audit(s, ctx, "webmail.access.granted")
    s.commit()
    return {"id": item.id, "user_id": item.user_id, "role": item.role}


@router.delete("/{mailbox_id}/access/{user_id}", status_code=204)
def revoke_access(mailbox_id: str, user_id: str, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _manager(ctx)
    mailbox = _mailbox(s, ctx, mailbox_id)
    item = s.scalar(select(WebmailAccess).where(WebmailAccess.mailbox_id == mailbox.id, WebmailAccess.user_id == user_id))
    if not item:
        raise HTTPException(404, "mailbox_access_not_found")
    s.delete(item)
    audit(s, ctx, "webmail.access.revoked")
    s.commit()
