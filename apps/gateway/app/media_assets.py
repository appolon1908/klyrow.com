"""Tenant-isolated media metadata and provider-neutral object lifecycle."""
import hashlib
import io
import json
import os
import re
import secrets
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError, DecompressionBombWarning
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, audit, db
from .tenancy import ROLE_PERMISSIONS

router = APIRouter(tags=["Tenant media"])

MAX_SIZE_BYTES = 10 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXEL_COUNT = MAX_DIMENSION * MAX_DIMENSION
ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}
STATUSES = {
    "PENDING_UPLOAD", "UPLOADED", "VALIDATING", "READY", "REJECTED", "QUARANTINED", "ARCHIVED", "DELETED"
}
TRANSITIONS = {
    "PENDING_UPLOAD": {"UPLOADED", "REJECTED", "QUARANTINED"},
    "UPLOADED": {"VALIDATING", "REJECTED", "QUARANTINED"},
    "VALIDATING": {"READY", "REJECTED", "QUARANTINED"},
    "READY": {"ARCHIVED", "QUARANTINED", "DELETED"},
    "REJECTED": set(), "QUARANTINED": {"DELETED"}, "ARCHIVED": {"DELETED"}, "DELETED": set(),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MediaObjectStore(Protocol):
    def prepare_upload(self, command: dict[str, Any]) -> dict[str, Any]: ...
    def put_object(self, command: dict[str, Any]) -> dict[str, Any]: ...
    def inspect_object(self, reference: str) -> dict[str, Any]: ...
    def finalize_object(self, command: dict[str, Any]) -> dict[str, Any]: ...
    def create_download_reference(self, command: dict[str, Any]) -> str: ...
    def quarantine_object(self, command: dict[str, Any]) -> None: ...
    def delete_object(self, command: dict[str, Any]) -> None: ...


@dataclass
class _FakeObject:
    tenant_id: str
    asset_id: str
    content: bytes
    expires_at: datetime
    object_key: str


class FakeMediaObjectStore:
    """Deterministic in-process adapter for tests and local development only."""

    def __init__(self) -> None:
        self.objects: dict[str, _FakeObject] = {}
        self.references: dict[str, tuple[str, datetime, str]] = {}

    def prepare_upload(self, command: dict[str, Any]) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(minutes=10)
        object_key = f"tenants/{command['tenant_id']}/media/{command['asset_id']}"
        self.references[token] = (command["tenant_id"], expires_at, command["asset_id"])
        return {"upload_reference": token, "expires_at": expires_at, "object_key": object_key}

    def put_test_object(self, upload_reference: str, content: bytes) -> None:
        item = self.references.get(upload_reference)
        if not item or item[1] <= utcnow():
            raise ValueError("expired_upload_reference")
        tenant_id, expires_at, asset_id = item
        self.objects[upload_reference] = _FakeObject(tenant_id, asset_id, content, expires_at, f"tenants/{tenant_id}/media/{asset_id}")

    def put_object(self, command: dict[str, Any]) -> dict[str, Any]:
        reference = str(command["upload_reference"])
        item = self.references.get(reference)
        if not item or item[1] <= utcnow():
            raise HTTPException(409, "upload_reference_expired")
        if item[0] != command["tenant_id"] or item[2] != command["asset_id"]:
            raise HTTPException(403, "object_reference_forbidden")
        content = bytes(command["content"])
        self.objects[reference] = _FakeObject(item[0], item[2], content, item[1], f"tenants/{item[0]}/media/{item[2]}")
        return {"size_bytes": len(content)}

    def _get(self, reference: str) -> _FakeObject:
        item = self.objects.get(reference)
        if not item or item.expires_at <= utcnow():
            raise HTTPException(409, "upload_reference_expired")
        return item

    def inspect_object(self, reference: str) -> dict[str, Any]:
        item = self._get(reference)
        return {"content": item.content, "size_bytes": len(item.content), "object_key": item.object_key}

    def finalize_object(self, command: dict[str, Any]) -> dict[str, Any]:
        item = self._get(command["upload_reference"])
        if item.tenant_id != command["tenant_id"] or item.asset_id != command["asset_id"]:
            raise HTTPException(403, "object_reference_forbidden")
        return {"object_key": item.object_key}

    def create_download_reference(self, command: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(24)
        self.references[token] = (command["tenant_id"], utcnow() + timedelta(minutes=5), command["asset_id"])
        return token

    def quarantine_object(self, command: dict[str, Any]) -> None:
        return None

    def delete_object(self, command: dict[str, Any]) -> None:
        reference = command.get("upload_reference", "")
        self.objects.pop(reference, None)
        self.references.pop(reference, None)


class UnavailableMediaObjectStore:
    def __getattr__(self, name: str):
        def unavailable(*args, **kwargs):
            raise HTTPException(503, "media_storage_unavailable")
        return unavailable


_fake_store = FakeMediaObjectStore()


def object_store() -> MediaObjectStore:
    if os.getenv("KLYROW_ENV", "development").lower() == "production":
        return UnavailableMediaObjectStore()
    return _fake_store


class MediaAsset(Base):
    __tablename__ = "klyrow_media_assets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    original_filename: Mapped[str] = mapped_column(String)
    safe_filename: Mapped[str] = mapped_column(String)
    media_kind: Mapped[str] = mapped_column(String)
    declared_content_type: Mapped[str] = mapped_column(String)
    detected_content_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    storage_provider: Mapped[str] = mapped_column(String, default="provider-neutral")
    storage_object_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="PENDING_UPLOAD", index=True)
    quarantine_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    upload_reference: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    __table_args__ = (
        CheckConstraint("status IN ('PENDING_UPLOAD','UPLOADED','VALIDATING','READY','REJECTED','QUARANTINED','ARCHIVED','DELETED')", name="ck_media_asset_status"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_media_asset_tenant_idempotency"),
    )


class MediaAssetEvent(Base):
    __tablename__ = "klyrow_media_asset_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("klyrow_media_assets.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    from_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    to_status: Mapped[str] = mapped_column(String)
    reason_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    correlation_id: Mapped[str] = mapped_column(String, index=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UploadIn(BaseModel):
    original_filename: str = Field(min_length=1, max_length=255)
    media_kind: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    declared_content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0, le=MAX_SIZE_BYTES)
    sha256_digest: Optional[str] = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("original_filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        if "\x00" in value or "\\" in value or "/" in value or value in {".", ".."}:
            raise ValueError("unsafe_filename")
        return value


class CompleteIn(BaseModel):
    upload_reference: str = Field(min_length=20, max_length=200)
    expected_version: int = Field(default=1, ge=1)


class MutationIn(BaseModel):
    expected_version: int = Field(default=1, ge=1)
    reason: Optional[str] = Field(default=None, max_length=200)


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "csrf_origin_denied")


def browser_context_dependency(request: Request, s: Session = Depends(db)) -> dict[str, Any]:
    from .auth_bff import browser_context
    return browser_context(request=request, s=s)


def csrf_dependency(request: Request, x_klyrow_csrf: str = Header(default="", alias="X-Klyrow-CSRF"), s: Session = Depends(db)) -> Any:
    from .auth_bff import csrf_guard
    return csrf_guard(request=request, x_klyrow_csrf=x_klyrow_csrf, s=s)


def require_mutation(ctx: dict[str, Any]) -> None:
    permissions = ROLE_PERMISSIONS.get(str(ctx.get("role", "")).upper(), set())
    if "*" not in permissions and "campaign.manage" not in permissions:
        raise HTTPException(403, "media_management_denied")


def safe_name(filename: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")[:160]
    return value or "media"


def dimensions_and_type(content: bytes) -> tuple[str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                detected = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
                if detected is None:
                    raise ValueError("unsupported_or_malformed_image")
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise ValueError("invalid_dimensions")
                if width > MAX_DIMENSION or height > MAX_DIMENSION:
                    raise ValueError("dimensions_exceeded")
                if width * height > MAX_PIXEL_COUNT:
                    raise ValueError("pixel_count_exceeded")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                if image.size != (width, height):
                    raise ValueError("malformed_image")
                return detected, width, height
    except (DecompressionBombError, DecompressionBombWarning, UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("malformed_image") from exc


def validate_object(content: bytes, declared: str, expected_size: int, expected_digest: Optional[str]) -> dict[str, Any]:
    if not content or len(content) == 0:
        raise ValueError("zero_byte_file")
    if len(content) > MAX_SIZE_BYTES or len(content) != expected_size:
        raise ValueError("size_mismatch_or_oversized")
    if b"<svg" in content[:4096].lower() or b"<script" in content[:4096].lower() or b"<html" in content[:4096].lower():
        raise ValueError("executable_or_markup_content")
    detected, width, height = dimensions_and_type(content)
    if detected not in ALLOWED_TYPES or declared != detected:
        raise ValueError("content_type_mismatch")
    digest = hashlib.sha256(content).hexdigest()
    if expected_digest and digest != expected_digest.lower():
        raise ValueError("digest_mismatch")
    return {"detected_content_type": detected, "width": width, "height": height, "sha256_digest": digest, "size_bytes": len(content)}


def payload(item: MediaAsset) -> dict[str, Any]:
    return {key: getattr(item, key) for key in ("id", "tenant_id", "original_filename", "safe_filename", "media_kind", "declared_content_type", "detected_content_type", "size_bytes", "sha256_digest", "width", "height", "storage_provider", "status", "quarantine_reason", "created_by", "created_at", "updated_at", "validated_at", "ready_at", "archived_at", "deleted_at", "version")}


def event(s: Session, item: MediaAsset, event_type: str, to_status: str, ctx: dict[str, Any], reason: Optional[str] = None, metadata: Optional[dict[str, Any]] = None, from_status: Optional[str] = None) -> None:
    s.add(MediaAssetEvent(id=str(uuid.uuid4()), asset_id=item.id, tenant_id=item.tenant_id, event_type=event_type, from_status=from_status, to_status=to_status, reason_code=reason, metadata_json=json.dumps(metadata or {}, sort_keys=True), correlation_id=str(uuid.uuid4()), created_by=ctx["sub"]))


def find_asset(s: Session, asset_id: str, tenant_id: str, lock: bool = False) -> MediaAsset:
    query = select(MediaAsset).where(MediaAsset.id == asset_id, MediaAsset.tenant_id == tenant_id)
    if lock:
        query = query.with_for_update()
    item = s.scalar(query)
    if not item:
        raise HTTPException(404, "media_asset_not_found")
    return item


def transition(s: Session, item: MediaAsset, target: str, ctx: dict[str, Any], reason: Optional[str] = None) -> None:
    if target not in TRANSITIONS.get(item.status, set()):
        raise HTTPException(409, "invalid_media_lifecycle_transition")
    previous = item.status
    item.status = target
    item.version += 1
    item.updated_at = utcnow()
    if target == "VALIDATING": item.validated_at = utcnow()
    if target == "READY": item.ready_at = utcnow()
    if target == "ARCHIVED": item.archived_at = utcnow()
    if target == "DELETED": item.deleted_at = utcnow()
    event(s, item, "lifecycle", target, ctx, reason, {"previous_status": previous}, from_status=previous)


@router.get("/app/api/media")
def media_list(ctx=Depends(browser_context_dependency), s: Session = Depends(db), status: Optional[str] = Query(None), media_type: Optional[str] = Query(None, alias="type"), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    query = select(MediaAsset).where(MediaAsset.tenant_id == ctx["tenant"], MediaAsset.status != "DELETED")
    if status:
        if status not in STATUSES: raise HTTPException(422, "invalid_media_status")
        query = query.where(MediaAsset.status == status)
    if media_type:
        query = query.where(MediaAsset.detected_content_type == media_type)
    rows = s.scalars(query.order_by(MediaAsset.created_at.desc()).offset(offset).limit(limit)).all()
    return {"items": [payload(row) for row in rows], "limit": limit, "offset": offset, "has_more": len(rows) == limit}


@router.post("/app/api/media/uploads", status_code=201)
def media_upload_prepare(x: UploadIn, ctx=Depends(browser_context_dependency), _session=Depends(csrf_dependency), s: Session = Depends(db), idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", min_length=8, max_length=200)):
    require_mutation(ctx)
    if x.declared_content_type not in ALLOWED_TYPES:
        raise HTTPException(422, "unsupported_media_type")
    if idempotency_key:
        prior = s.scalar(select(MediaAsset).where(MediaAsset.tenant_id == ctx["tenant"], MediaAsset.idempotency_key == idempotency_key))
        if prior:
            if (prior.original_filename, prior.media_kind, prior.declared_content_type, prior.size_bytes) != (x.original_filename, x.media_kind, x.declared_content_type, x.size_bytes) or (x.sha256_digest is not None and prior.sha256_digest != x.sha256_digest):
                raise HTTPException(409, "media_upload_idempotency_conflict")
            return {**payload(prior), "upload_reference": prior.upload_reference, "duplicate": True}
    item = MediaAsset(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], original_filename=x.original_filename, safe_filename=safe_name(x.original_filename), media_kind=x.media_kind, declared_content_type=x.declared_content_type, size_bytes=x.size_bytes, sha256_digest=x.sha256_digest, idempotency_key=idempotency_key, storage_provider="fake" if isinstance(object_store(), FakeMediaObjectStore) else "unavailable", status="PENDING_UPLOAD", created_by=ctx["sub"])
    prepared = object_store().prepare_upload({"tenant_id": item.tenant_id, "asset_id": item.id})
    item.upload_reference = prepared["upload_reference"]
    s.add(item); event(s, item, "upload_prepared", "PENDING_UPLOAD", ctx, metadata={"expires_at": prepared["expires_at"].isoformat()}); audit(s, ctx, "media.upload.prepared"); s.commit()
    return {**payload(item), "upload_reference": prepared["upload_reference"], "upload_expires_at": prepared["expires_at"], "duplicate": False}


@router.put("/app/api/media/uploads/{upload_reference}")
async def media_upload_bytes(upload_reference: str, request: Request, ctx=Depends(browser_context_dependency), _session=Depends(csrf_dependency), s: Session = Depends(db)):
    require_mutation(ctx)
    item = s.scalar(select(MediaAsset).where(MediaAsset.upload_reference == upload_reference, MediaAsset.tenant_id == ctx["tenant"], MediaAsset.status == "PENDING_UPLOAD"))
    if not item:
        raise HTTPException(404, "media_upload_not_found")
    content = bytearray()
    received = 0
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_SIZE_BYTES or int(content_length) != item.size_bytes:
                raise HTTPException(422, "size_mismatch_or_oversized")
        except ValueError as exc:
            raise HTTPException(422, "invalid_content_length") from exc
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_SIZE_BYTES or received > item.size_bytes:
            raise HTTPException(422, "size_mismatch_or_oversized")
        content.extend(chunk)
    if not content or received != item.size_bytes:
        raise HTTPException(422, "size_mismatch_or_oversized")
    result = object_store().put_object({"tenant_id": item.tenant_id, "asset_id": item.id, "upload_reference": upload_reference, "content": bytes(content)})
    return {"upload_reference": upload_reference, "size_bytes": result["size_bytes"]}


@router.post("/app/api/media/{asset_id}/complete")
def media_complete(asset_id: str, x: CompleteIn, ctx=Depends(browser_context_dependency), _session=Depends(csrf_dependency), s: Session = Depends(db)):
    require_mutation(ctx)
    item = find_asset(s, asset_id, ctx["tenant"], lock=True)
    if item.status == "READY": return {**payload(item), "duplicate": True}
    if item.status != "PENDING_UPLOAD" or item.upload_reference != x.upload_reference:
        raise HTTPException(409, "media_upload_not_pending")
    def cleanup_upload() -> None:
        try:
            object_store().delete_object({"tenant_id": item.tenant_id, "asset_id": item.id, "upload_reference": x.upload_reference})
        except Exception:
            pass

    try:
        inspected = object_store().inspect_object(x.upload_reference)
        details = validate_object(inspected["content"], item.declared_content_type, item.size_bytes, item.sha256_digest)
    except (ValueError, KeyError) as exc:
        item.status = "REJECTED"; item.quarantine_reason = str(exc); item.version += 1; item.updated_at = utcnow(); event(s, item, "upload_rejected", "REJECTED", ctx, reason=str(exc), from_status="PENDING_UPLOAD"); cleanup_upload(); s.commit(); raise HTTPException(422, str(exc))
    except Exception:
        cleanup_upload()
        raise
    if x.expected_version != item.version:
        raise HTTPException(409, "media_version_conflict")
    item.status = "UPLOADED"; item.version += 1; item.updated_at = utcnow(); event(s, item, "upload_completed", "UPLOADED", ctx, from_status="PENDING_UPLOAD")
    item.status = "VALIDATING"; item.version += 1; item.validated_at = utcnow(); item.updated_at = utcnow(); event(s, item, "validation_started", "VALIDATING", ctx, from_status="UPLOADED")
    try:
        finalized = object_store().finalize_object({"tenant_id": item.tenant_id, "asset_id": item.id, "upload_reference": x.upload_reference})
    except Exception:
        cleanup_upload()
        raise
    item.detected_content_type = details["detected_content_type"]; item.sha256_digest = details["sha256_digest"]; item.width = details["width"]; item.height = details["height"]; item.size_bytes = details["size_bytes"]; item.storage_object_key = finalized["object_key"]
    item.status = "READY"; item.ready_at = utcnow(); item.version += 1; item.updated_at = utcnow(); event(s, item, "validation_succeeded", "READY", ctx, metadata={"detected_content_type": item.detected_content_type}, from_status="VALIDATING"); audit(s, ctx, "media.upload.ready"); s.commit()
    return {**payload(item), "duplicate": False}


@router.get("/app/api/media/{asset_id}/events")
def media_events(asset_id: str, ctx=Depends(browser_context_dependency), s: Session = Depends(db)):
    find_asset(s, asset_id, ctx["tenant"])
    return {"items": [dict(id=e.id, asset_id=e.asset_id, tenant_id=e.tenant_id, event_type=e.event_type, from_status=e.from_status, to_status=e.to_status, reason_code=e.reason_code, metadata=json.loads(e.metadata_json), correlation_id=e.correlation_id, created_by=e.created_by, created_at=e.created_at) for e in s.scalars(select(MediaAssetEvent).where(MediaAssetEvent.asset_id == asset_id, MediaAssetEvent.tenant_id == ctx["tenant"]).order_by(MediaAssetEvent.created_at.asc())).all()]}


@router.get("/app/api/media/{asset_id}")
def media_get(asset_id: str, ctx=Depends(browser_context_dependency), s: Session = Depends(db)):
    item = find_asset(s, asset_id, ctx["tenant"])
    if item.status == "DELETED":
        raise HTTPException(404, "media_asset_not_found")
    return payload(item)


@router.post("/app/api/media/{asset_id}/archive")
def media_archive(asset_id: str, x: MutationIn, ctx=Depends(browser_context_dependency), _session=Depends(csrf_dependency), s: Session = Depends(db)):
    require_mutation(ctx); item = find_asset(s, asset_id, ctx["tenant"], lock=True)
    if x.expected_version != item.version: raise HTTPException(409, "media_version_conflict")
    transition(s, item, "ARCHIVED", ctx, x.reason); audit(s, ctx, "media.asset.archived"); s.commit(); return payload(item)


@router.delete("/app/api/media/{asset_id}")
def media_delete(asset_id: str, x: MutationIn = Body(default=MutationIn()), ctx=Depends(browser_context_dependency), _session=Depends(csrf_dependency), s: Session = Depends(db)):
    require_mutation(ctx); item = find_asset(s, asset_id, ctx["tenant"], lock=True)
    if x.expected_version != item.version: raise HTTPException(409, "media_version_conflict")
    upload_reference = item.upload_reference
    transition(s, item, "DELETED", ctx, x.reason); object_store().delete_object({"tenant_id": item.tenant_id, "asset_id": item.id, "upload_reference": upload_reference}); audit(s, ctx, "media.asset.deleted"); s.commit(); return payload(item)
