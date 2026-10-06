"""Tenant-scoped browser API for Brand Profiles.

Asset identifiers remain opaque until the Media Library becomes the canonical
asset authority. This module intentionally performs no storage or upload work.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth_bff import BrowserSession, browser_context, csrf_guard
from .capabilities import require_permission
from .main import Base, audit, db


router = APIRouter(tags=["Brand profiles"])
SAFE_FONTS = {"Arial", "Georgia", "Helvetica", "Inter", "Roboto", "Times New Roman", "Verdana"}
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
EDITABLE_FIELDS = (
    "name", "is_default", "company_name", "website_url", "support_email",
    "logo_asset_id", "icon_asset_id", "primary_color", "secondary_color",
    "accent_color", "background_color", "text_color", "heading_font", "body_font",
    "footer_text", "physical_address",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BrandProfile(Base):
    __tablename__ = "klyrow_brand_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    company_name: Mapped[str] = mapped_column(String)
    website_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    support_email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    logo_asset_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    icon_asset_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    primary_color: Mapped[str] = mapped_column(String)
    secondary_color: Mapped[str] = mapped_column(String)
    accent_color: Mapped[str] = mapped_column(String)
    background_color: Mapped[str] = mapped_column(String)
    text_color: Mapped[str] = mapped_column(String)
    heading_font: Mapped[str] = mapped_column(String)
    body_font: Mapped[str] = mapped_column(String)
    footer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    physical_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="DRAFT")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class BrandProfileVersion(Base):
    __tablename__ = "klyrow_brand_profile_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    brand_profile_id: Mapped[str] = mapped_column(String, index=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BrandInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    is_default: bool = False
    company_name: str = Field(min_length=1, max_length=200)
    website_url: Optional[str] = Field(default=None, max_length=2048)
    support_email: Optional[EmailStr] = None
    logo_asset_id: Optional[str] = Field(default=None, max_length=128)
    icon_asset_id: Optional[str] = Field(default=None, max_length=128)
    primary_color: str
    secondary_color: str
    accent_color: str
    background_color: str
    text_color: str
    heading_font: str
    body_font: str
    footer_text: Optional[str] = Field(default=None, max_length=4000)
    physical_address: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("website_url")
    @classmethod
    def approved_url_scheme(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("approved_url_scheme_required")
        return value

    @field_validator("primary_color", "secondary_color", "accent_color", "background_color", "text_color")
    @classmethod
    def normalized_color(cls, value: str) -> str:
        if not COLOR_RE.fullmatch(value):
            raise ValueError("normalized_hex_color_required")
        return value.upper()

    @field_validator("heading_font", "body_font")
    @classmethod
    def safe_font(cls, value: str) -> str:
        if value not in SAFE_FONTS:
            raise ValueError("unsupported_font")
        return value


class BrandPatch(BaseModel):
    version: int = Field(ge=1)
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    is_default: Optional[bool] = None
    company_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    website_url: Optional[str] = Field(default=None, max_length=2048)
    support_email: Optional[EmailStr] = None
    logo_asset_id: Optional[str] = Field(default=None, max_length=128)
    icon_asset_id: Optional[str] = Field(default=None, max_length=128)
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    accent_color: Optional[str] = None
    background_color: Optional[str] = None
    text_color: Optional[str] = None
    heading_font: Optional[str] = None
    body_font: Optional[str] = None
    footer_text: Optional[str] = Field(default=None, max_length=4000)
    physical_address: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("website_url")
    @classmethod
    def approved_url_scheme(cls, value: Optional[str]) -> Optional[str]:
        return BrandInput.approved_url_scheme(value)

    @field_validator("primary_color", "secondary_color", "accent_color", "background_color", "text_color")
    @classmethod
    def normalized_color(cls, value: Optional[str]) -> Optional[str]:
        return BrandInput.normalized_color(value) if value is not None else None

    @field_validator("heading_font", "body_font")
    @classmethod
    def safe_font(cls, value: Optional[str]) -> Optional[str]:
        return BrandInput.safe_font(value) if value is not None else None


def _read(ctx: dict) -> None:
    require_permission(ctx, "brand.read")


def _manage(ctx: dict) -> None:
    require_permission(ctx, "brand.manage")


def _profile_or_404(s: Session, tenant_id: str, brand_id: str) -> BrandProfile:
    profile = s.scalar(select(BrandProfile).where(BrandProfile.id == brand_id, BrandProfile.tenant_id == tenant_id))
    if not profile:
        raise HTTPException(404, "not_found")
    return profile


def _snapshot(profile: BrandProfile) -> dict:
    return {field: getattr(profile, field) for field in EDITABLE_FIELDS if field != "is_default"} | {
        "is_default": profile.is_default,
        "logo_asset_id": profile.logo_asset_id,
        "icon_asset_id": profile.icon_asset_id,
    }


def _serialize(profile: BrandProfile) -> dict:
    return {
        "id": profile.id, "tenant_id": profile.tenant_id, "name": profile.name,
        "is_default": profile.is_default, "company_name": profile.company_name,
        "website_url": profile.website_url, "support_email": profile.support_email,
        "logo_asset_id": profile.logo_asset_id, "icon_asset_id": profile.icon_asset_id,
        "primary_color": profile.primary_color, "secondary_color": profile.secondary_color,
        "accent_color": profile.accent_color, "background_color": profile.background_color,
        "text_color": profile.text_color, "heading_font": profile.heading_font,
        "body_font": profile.body_font, "footer_text": profile.footer_text,
        "physical_address": profile.physical_address, "status": profile.status,
        "created_at": profile.created_at, "updated_at": profile.updated_at,
        "published_at": profile.published_at, "version": profile.version,
    }


def _set_default(s: Session, profile: BrandProfile) -> None:
    if not profile.is_default:
        return
    if profile.status == "ARCHIVED":
        raise HTTPException(422, "archived_brand_cannot_be_default")
    for item in s.scalars(select(BrandProfile).where(
        BrandProfile.tenant_id == profile.tenant_id,
        BrandProfile.id != profile.id,
        BrandProfile.is_default == True,
        BrandProfile.status == "ACTIVE",
    )):
        item.is_default = False


def _append_version(s: Session, profile: BrandProfile, actor: str) -> BrandProfileVersion:
    previous = s.scalar(select(BrandProfileVersion.version_number).where(
        BrandProfileVersion.brand_profile_id == profile.id,
        BrandProfileVersion.tenant_id == profile.tenant_id,
    ).order_by(BrandProfileVersion.version_number.desc()))
    item = BrandProfileVersion(
        id=str(uuid.uuid4()), brand_profile_id=profile.id, tenant_id=profile.tenant_id,
        version_number=(previous or 0) + 1,
        snapshot_json=json.dumps(_snapshot(profile), sort_keys=True, separators=(",", ":")),
        created_by=actor,
    )
    s.add(item)
    return item


@router.get("/app/api/brands")
def list_brands(ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    _read(ctx)
    items = s.scalars(select(BrandProfile).where(BrandProfile.tenant_id == ctx["tenant"]).order_by(BrandProfile.created_at.desc())).all()
    return {"items": [_serialize(item) for item in items]}


@router.post("/app/api/brands", status_code=201)
def create_brand(payload: BrandInput, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _manage(ctx)
    values = payload.model_dump()
    profile = BrandProfile(id=str(uuid.uuid4()), tenant_id=ctx["tenant"], created_by=ctx["sub"], **values)
    _set_default(s, profile)
    s.add(profile)
    audit(s, ctx, "brand.created")
    s.commit()
    s.refresh(profile)
    return _serialize(profile)


@router.get("/app/api/brands/{brand_id}")
def get_brand(brand_id: str, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    _read(ctx)
    return _serialize(_profile_or_404(s, ctx["tenant"], brand_id))


@router.patch("/app/api/brands/{brand_id}")
def update_brand(brand_id: str, payload: BrandPatch, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _manage(ctx)
    profile = _profile_or_404(s, ctx["tenant"], brand_id)
    if profile.status == "ARCHIVED":
        raise HTTPException(409, "archived_brand_immutable")
    if payload.version != profile.version:
        raise HTTPException(409, "brand_version_conflict")
    for field in payload.model_fields_set - {"version"}:
        setattr(profile, field, getattr(payload, field))
    _set_default(s, profile)
    profile.version += 1
    profile.updated_at = _now()
    audit(s, ctx, "brand.updated")
    s.commit()
    return _serialize(profile)


@router.post("/app/api/brands/{brand_id}/publish")
def publish_brand(brand_id: str, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _manage(ctx)
    profile = _profile_or_404(s, ctx["tenant"], brand_id)
    if profile.status == "ARCHIVED":
        raise HTTPException(409, "archived_brand_immutable")
    if profile.status == "ACTIVE":
        return _serialize(profile)
    profile.status = "ACTIVE"
    profile.published_at = _now()
    profile.updated_at = profile.published_at
    profile.version += 1
    _set_default(s, profile)
    _append_version(s, profile, ctx["sub"])
    audit(s, ctx, "brand.published")
    s.commit()
    return _serialize(profile)


@router.get("/app/api/brands/{brand_id}/versions")
def list_brand_versions(brand_id: str, ctx: dict = Depends(browser_context), s: Session = Depends(db)):
    _read(ctx)
    _profile_or_404(s, ctx["tenant"], brand_id)
    items = s.scalars(select(BrandProfileVersion).where(
        BrandProfileVersion.brand_profile_id == brand_id,
        BrandProfileVersion.tenant_id == ctx["tenant"],
    ).order_by(BrandProfileVersion.version_number.desc())).all()
    return {"items": [{"version": item.version_number, "snapshot": json.loads(item.snapshot_json), "created_at": item.created_at, "created_by": item.created_by} for item in items]}


@router.post("/app/api/brands/{brand_id}/versions/{version}/restore")
def restore_brand_version(brand_id: str, version: int, ctx: dict = Depends(browser_context), _session: BrowserSession = Depends(csrf_guard), s: Session = Depends(db)):
    _manage(ctx)
    profile = _profile_or_404(s, ctx["tenant"], brand_id)
    if profile.status == "ARCHIVED":
        raise HTTPException(409, "archived_brand_immutable")
    historical = s.scalar(select(BrandProfileVersion).where(
        BrandProfileVersion.brand_profile_id == brand_id,
        BrandProfileVersion.tenant_id == ctx["tenant"],
        BrandProfileVersion.version_number == version,
    ))
    if not historical:
        raise HTTPException(404, "not_found")
    for field, value in json.loads(historical.snapshot_json).items():
        if field in EDITABLE_FIELDS:
            setattr(profile, field, value)
    profile.status = "ACTIVE"
    profile.published_at = _now()
    profile.updated_at = profile.published_at
    profile.version += 1
    _set_default(s, profile)
    _append_version(s, profile, ctx["sub"])
    audit(s, ctx, "brand.version_restored")
    s.commit()
    return _serialize(profile)