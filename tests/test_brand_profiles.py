from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.auth_bff import browser_context, csrf_guard
from apps.gateway.app.brand_profiles import BrandProfile, BrandProfileVersion, router as brand_router
from apps.gateway.app.main import Base, db


@pytest.fixture
def brand_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    BrandProfile.__table__.create(engine)
    BrandProfileVersion.__table__.create(engine)

    def session_dependency():
        with Session(engine) as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[db] = session_dependency
    app.dependency_overrides[browser_context] = lambda: {"tenant": "tenant-a", "sub": "owner-a", "role": "OWNER"}
    app.dependency_overrides[csrf_guard] = lambda: object()
    app.dependency_overrides[platform_owner_role_stability_guard] = lambda: None
    for route in app.routes:
        if not getattr(route, "path", "").startswith("/app/api/brands"):
            continue
        for dependency in route.dependant.dependencies:
            if dependency.call and dependency.call.__name__ == "platform_owner_role_stability_guard":
                app.dependency_overrides[dependency.call] = lambda: None
    client = TestClient(app, base_url="https://app.klyrow.test")
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        engine.dispose()


def _draft(**overrides):
    return {
        "name": "Primary brand",
        "company_name": "Klyrow Ltd",
        "website_url": "https://klyrow.example",
        "support_email": "support@klyrow.example",
        "primary_color": "#11aa22",
        "secondary_color": "#ffffff",
        "accent_color": "#123abc",
        "background_color": "#ffffff",
        "text_color": "#101010",
        "heading_font": "Inter",
        "body_font": "Inter",
        "footer_text": "Klyrow",
        "physical_address": "1 Example Street",
        "is_default": True,
        **overrides,
    }


def test_draft_normalizes_and_is_tenant_scoped(brand_client):
    created = brand_client.post("/app/api/brands", json=_draft())
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["tenant_id"] == "tenant-a"
    assert item["primary_color"] == "#11AA22"
    assert item["status"] == "DRAFT"
    assert item["logo_asset_id"] is None
    assert brand_client.get("/app/api/brands").json()["items"][0]["id"] == item["id"]


@pytest.mark.parametrize("field,value", [
    ("website_url", "javascript:alert(1)"),
    ("support_email", "not-an-email"),
    ("primary_color", "red"),
    ("heading_font", "Unsafe Font"),
])
def test_invalid_brand_input_is_rejected(brand_client, field, value):
    assert brand_client.post("/app/api/brands", json=_draft(**{field: value})).status_code == 422


def test_publish_is_idempotent_and_restore_creates_immutable_version(brand_client):
    created = brand_client.post("/app/api/brands", json=_draft()).json()
    brand_id = created["id"]
    updated = brand_client.patch(f"/app/api/brands/{brand_id}", json={"version": created["version"], "company_name": "Klyrow Systems"})
    assert updated.status_code == 200, updated.text
    assert brand_client.patch(f"/app/api/brands/{brand_id}", json={"version": created["version"], "company_name": "Stale"}).status_code == 409

    published = brand_client.post(f"/app/api/brands/{brand_id}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "ACTIVE"
    assert brand_client.post(f"/app/api/brands/{brand_id}/publish").json()["version"] == published.json()["version"]
    history = brand_client.get(f"/app/api/brands/{brand_id}/versions").json()["items"]
    assert len(history) == 1 and history[0]["snapshot"]["company_name"] == "Klyrow Systems"

    restored = brand_client.post(f"/app/api/brands/{brand_id}/versions/1/restore")
    assert restored.status_code == 200
    assert len(brand_client.get(f"/app/api/brands/{brand_id}/versions").json()["items"]) == 2


def test_cross_tenant_ids_are_not_disclosed(brand_client):
    created = brand_client.post("/app/api/brands", json=_draft()).json()
    previous = app.dependency_overrides[browser_context]
    app.dependency_overrides[browser_context] = lambda: {"tenant": "tenant-b", "sub": "owner-b", "role": "OWNER"}
    try:
        assert brand_client.get(f"/app/api/brands/{created['id']}").status_code == 404
        assert brand_client.patch(f"/app/api/brands/{created['id']}", json={"version": 1, "company_name": "Nope"}).status_code == 404
    finally:
        app.dependency_overrides[browser_context] = previous