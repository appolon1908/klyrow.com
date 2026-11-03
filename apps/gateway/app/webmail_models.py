"""Persistence models for the Klyrow webmail suite.

This module intentionally depends only on the core ORM registry.  Keeping it
free of browser-auth and route imports makes schema registration safe for API,
worker, migration, and test entry points regardless of import order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base

now = lambda: datetime.now(UTC)


class WebmailMailbox(Base):
    __tablename__ = "webmail_mailboxes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "address", name="uq_webmail_mailbox_tenant_address"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    domain_id: Mapped[str] = mapped_column(String, index=True)
    address: Mapped[str] = mapped_column(String, index=True)
    display_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", index=True)
    sending_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    receiving_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    storage_quota_bytes: Mapped[int] = mapped_column(Integer, default=1_073_741_824)
    storage_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WebmailAccess(Base):
    __tablename__ = "webmail_access"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "user_id", name="uq_webmail_access_mailbox_user"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("webmail_mailboxes.id"), index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String, default="READER")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WebmailMessage(Base):
    __tablename__ = "webmail_messages"
    __table_args__ = (
        UniqueConstraint("provider_inbound_id", name="uq_webmail_message_provider_inbound"),
        UniqueConstraint("outbound_message_id", name="uq_webmail_message_outbound"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("webmail_mailboxes.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String, index=True)
    provider_inbound_id: Mapped[str | None] = mapped_column(String, nullable=True)
    outbound_message_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String, index=True)
    folder: Mapped[str] = mapped_column(String, index=True)
    message_id_header: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String, nullable=True)
    reply_to_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    references_json: Mapped[str] = mapped_column(Text, default="[]")
    from_address: Mapped[str] = mapped_column(String)
    to_json: Mapped[str] = mapped_column(Text, default="[]")
    cc_json: Mapped[str] = mapped_column(Text, default="[]")
    bcc_json: Mapped[str] = mapped_column(Text, default="[]")
    reply_to: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[str] = mapped_column(String, default="")
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments_json: Mapped[str] = mapped_column(Text, default="[]")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    delivery_status: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WebmailAttachment(Base):
    __tablename__ = "webmail_attachments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("webmail_mailboxes.id"), index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("webmail_messages.id"), index=True)
    filename: Mapped[str] = mapped_column(String)
    content_type: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
