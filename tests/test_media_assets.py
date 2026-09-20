from datetime import datetime, timedelta, timezone
from io import BytesIO
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from apps.gateway.app.auth_bff import BrowserSession, SESSION_COOKIE
from apps.gateway.app.main import Base, DB, Tenant, User, app, engine, ph, sha
from apps.gateway.app.media_assets import FakeMediaObjectStore, _fake_store
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

pytestmark = pytest.mark.usefixtures("isolated_durable_result_keyring")

client = TestClient(app)
tokens = {}
upload_counter = 0

def encoded_image(format_name: str, mode: str = "RGB", size: tuple[int, int] = (1, 1)) -> bytes:
    image = Image.new(mode, size, (12, 34, 56, 128) if mode == "RGBA" else (12, 34, 56))
    output = BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


PNG_1X1 = encoded_image("PNG")
JPEG_1X1 = encoded_image("JPEG")
WEBP_1X1 = encoded_image("WEBP")
TRANSPARENT_WEBP_1X1 = encoded_image("WEBP", "RGBA")


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        for tenant_id in ("media-a", "media-b"):
            session.add(Tenant(id=tenant_id, name=tenant_id, quota=10000))
            session.add(User(id=tenant_id, tenant_id=tenant_id, email=f"{tenant_id}@example.com", password_hash=ph.hash("long-enough-password"), role="tenant_admin"))
            session.add(OidcIdentity(id=f"identity-{tenant_id}", issuer="https://media.test", subject=f"subject-{tenant_id}", user_id=tenant_id, default_tenant_id=tenant_id, identity_type="KLYROW_ONLY", enabled=True))
            session.add(TenantMember(id=f"member-{tenant_id}", tenant_id=tenant_id, user_id=tenant_id, role="OWNER", active=True))
            session.add(BrowserSession(id=f"session-{tenant_id}", token_hash=sha(f"token-{tenant_id}"), csrf_hash=sha(f"csrf-{tenant_id}"), identity_id=f"identity-{tenant_id}", user_id=tenant_id, tenant_id=tenant_id, role="OWNER", created_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
        session.commit()
    _fake_store.objects.clear()
    _fake_store.references.clear()
    tokens.clear()
    global upload_counter
    upload_counter = 0


def login(tenant_id):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, f"token-{tenant_id}", path="/")
    tokens[tenant_id] = {"X-Klyrow-CSRF": f"csrf-{tenant_id}"}
    return tokens[tenant_id]


def prepare(tenant_id="media-a", filename="logo.png", content=PNG_1X1, declared="image/png", key=None):
    global upload_counter
    upload_counter += 1
    key = key or f"key-{tenant_id}-{filename}-{upload_counter}"
    response = client.post(
        "/app/api/media/uploads",
        headers={**login(tenant_id), "Idempotency-Key": key},
        json={"original_filename": filename, "media_kind": "image", "declared_content_type": declared, "size_bytes": len(content)},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    _fake_store.put_test_object(result["upload_reference"], content)
    return result


def test_media_upload_validates_bytes_and_records_immutable_events():
    prepared = prepare(key="key-media-a-idempotent")
    complete = client.post(
        f"/app/api/media/{prepared['id']}/complete",
        headers=login("media-a"),
        json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
    )
    assert complete.status_code == 200, complete.text
    asset = complete.json()
    assert asset["status"] == "READY"
    assert asset["storage_object_key" if "storage_object_key" in asset else "id"]
    events = client.get(f"/app/api/media/{asset['id']}/events", headers=login("media-a"))
    assert events.status_code == 200
    assert [item["to_status"] for item in events.json()["items"]][-1] == "READY"

    duplicate = client.post(
        f"/app/api/media/{asset['id']}/complete",
        headers=login("media-a"),
        json={"upload_reference": prepared["upload_reference"], "expected_version": asset["version"]},
    )
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True

    retry = client.post(
        "/app/api/media/uploads",
        headers={**login("media-a"), "Idempotency-Key": "key-media-a-idempotent"},
        json={"original_filename": "logo.png", "media_kind": "image", "declared_content_type": "image/png", "size_bytes": len(PNG_1X1)},
    )
    assert retry.status_code == 201 and retry.json()["duplicate"] is True


def test_media_is_tenant_scoped_and_only_ready_assets_are_listed_by_filter():
    prepared = prepare()
    cross_tenant = client.get(f"/app/api/media/{prepared['id']}", headers=login("media-b"))
    assert cross_tenant.status_code == 404
    listing = client.get("/app/api/media?status=READY", headers=login("media-a"))
    assert listing.status_code == 200
    assert all(item["status"] == "READY" for item in listing.json()["items"])


def test_media_rejects_mismatched_content_and_path_traversal():
    traversal = client.post(
        "/app/api/media/uploads",
        headers={**login("media-a"), "Origin": "http://testserver"},
        json={"original_filename": "..\\evil.png", "media_kind": "image", "declared_content_type": "image/png", "size_bytes": len(PNG_1X1)},
    )
    assert traversal.status_code == 422
    bad = prepare(filename="bad.png", content=b"<html><script>alert(1)</script>", declared="image/png")
    result = client.post(
        f"/app/api/media/{bad['id']}/complete",
        headers={**login("media-a"), "Origin": "http://testserver"},
        json={"upload_reference": bad["upload_reference"], "expected_version": bad["version"]},
    )
    assert result.status_code == 422
    assert result.json()["detail"] in {"executable_or_markup_content", "unsupported_or_malformed_image"}


def test_media_mutations_require_same_origin_and_optimistic_version():
    prepared = prepare()
    csrf = client.post(
        f"/app/api/media/{prepared['id']}/complete",
        headers={"X-Klyrow-CSRF": "wrong-token"},
        json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
    )
    assert csrf.status_code == 403
    _fake_store.put_test_object(prepared["upload_reference"], PNG_1X1)
    complete = client.post(
        f"/app/api/media/{prepared['id']}/complete",
        headers=login("media-a"),
        json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
    )
    assert complete.status_code == 200
    archive = client.post(
        f"/app/api/media/{prepared['id']}/archive",
        headers=login("media-a"),
        json={"expected_version": prepared["version"]},
    )
    assert archive.status_code == 409


def test_media_invalid_declared_type_is_rejected_before_storage():
    result = client.post(
        "/app/api/media/uploads",
        headers={**login("media-a"), "Origin": "http://testserver"},
        json={"original_filename": "vector.svg", "media_kind": "image", "declared_content_type": "image/svg+xml", "size_bytes": 10},
    )
    assert result.status_code == 422
    assert result.json()["detail"] == "unsupported_media_type"


@pytest.mark.parametrize("role", ["DEVELOPER", "BILLING", "SUPPORT", "ANALYST"])
def test_media_mutations_require_campaign_manage(role):
    with DB() as session:
        member = session.scalar(select(TenantMember).where(TenantMember.tenant_id == "media-a", TenantMember.user_id == "media-a"))
        member.role = role
        session.commit()
    try:
        response = client.post(
            "/app/api/media/uploads",
            headers={**login("media-a"), "Idempotency-Key": f"denied-{role.lower()}"},
            json={"original_filename": "denied.png", "media_kind": "image", "declared_content_type": "image/png", "size_bytes": len(PNG_1X1)},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "media_management_denied"
    finally:
        with DB() as session:
            member = session.scalar(select(TenantMember).where(TenantMember.tenant_id == "media-a", TenantMember.user_id == "media-a"))
            member.role = "OWNER"
            session.commit()


def test_media_mutation_requires_authentication():
    client.cookies.clear()
    response = client.post(
        "/app/api/media/uploads",
        headers={"Idempotency-Key": "unauthenticated-media"},
        json={"original_filename": "denied.png", "media_kind": "image", "declared_content_type": "image/png", "size_bytes": len(PNG_1X1)},
    )
    assert response.status_code == 401


def test_browser_upload_transmits_bytes_before_completion():
    prepared = prepare()
    uploaded = client.put(
        f"/app/api/media/uploads/{prepared['upload_reference']}",
        headers={**login("media-a"), "Content-Type": "image/png"},
        content=PNG_1X1,
    )
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        f"/app/api/media/{prepared['id']}/complete",
        headers=login("media-a"),
        json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "READY"


def test_media_archive_and_delete_require_campaign_manage():
    prepared = prepare()
    complete = client.post(
        f"/app/api/media/{prepared['id']}/complete",
        headers=login("media-a"),
        json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
    )
    assert complete.status_code == 200
    with DB() as session:
        member = session.scalar(select(TenantMember).where(TenantMember.tenant_id == "media-a", TenantMember.user_id == "media-a"))
        member.role = "BILLING"
        session.commit()
    try:
        archive = client.post(f"/app/api/media/{prepared['id']}/archive", headers=login("media-a"), json={"expected_version": complete.json()["version"]})
        deleted = client.request("DELETE", f"/app/api/media/{prepared['id']}", headers=login("media-a"), json={"expected_version": complete.json()["version"]})
        assert archive.status_code == 403
        assert deleted.status_code == 403
    finally:
        with DB() as session:
            member = session.scalar(select(TenantMember).where(TenantMember.tenant_id == "media-a", TenantMember.user_id == "media-a"))
            member.role = "OWNER"
            session.commit()


def test_fake_storage_delete_removes_uploaded_bytes():
    prepared = prepare()
    assert prepared["upload_reference"] in _fake_store.objects
    complete = client.post(
        f"/app/api/media/{prepared['id']}/complete",
        headers=login("media-a"),
        json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
    )
    assert complete.status_code == 200
    deleted = client.request("DELETE", f"/app/api/media/{prepared['id']}", headers=login("media-a"), json={"expected_version": complete.json()["version"]})
    assert deleted.status_code == 200
    assert prepared["upload_reference"] not in _fake_store.objects
    assert prepared["upload_reference"] not in _fake_store.references


def test_rejected_upload_cleans_fake_storage():
    prepared = prepare(filename="bad.png", content=b"bad", declared="image/png")
    rejected = client.post(
        f"/app/api/media/{prepared['id']}/complete",
        headers=login("media-a"),
        json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
    )
    assert rejected.status_code == 422
    assert prepared["upload_reference"] not in _fake_store.objects
    assert prepared["upload_reference"] not in _fake_store.references
    visible = client.get(f"/app/api/media/{prepared['id']}", headers=login("media-a"))
    assert visible.status_code == 200 and visible.json()["status"] == "REJECTED"


def test_storage_failure_cleans_staged_upload(monkeypatch):
    prepared = prepare()
    def fail_finalize(command):
        raise RuntimeError("storage_failure")
    monkeypatch.setattr(_fake_store, "finalize_object", fail_finalize)
    with pytest.raises(RuntimeError, match="storage_failure"):
        client.post(
            f"/app/api/media/{prepared['id']}/complete",
            headers=login("media-a"),
            json={"upload_reference": prepared["upload_reference"], "expected_version": prepared["version"]},
        )
    assert prepared["upload_reference"] not in _fake_store.objects
    assert prepared["upload_reference"] not in _fake_store.references


@pytest.mark.parametrize(
    ("content", "declared", "expected_type"),
    [
        (PNG_1X1, "image/png", "image/png"),
        (JPEG_1X1, "image/jpeg", "image/jpeg"),
        (WEBP_1X1, "image/webp", "image/webp"),
        (TRANSPARENT_WEBP_1X1, "image/webp", "image/webp"),
    ],
)
def test_real_images_are_decoder_validated(content, declared, expected_type):
    from apps.gateway.app.media_assets import validate_object

    result = validate_object(content, declared, len(content), None)
    assert result["detected_content_type"] == expected_type
    assert result["width"] == 1
    assert result["height"] == 1
    assert result["sha256_digest"]


@pytest.mark.parametrize("content", [PNG_1X1[:-8], JPEG_1X1[:-8], WEBP_1X1[:-8], b"RIFF\x00\x00\x00\x00WEBP"])
def test_decoder_rejects_truncated_or_fake_images(content):
    from apps.gateway.app.media_assets import validate_object

    with pytest.raises(ValueError, match="malformed_image"):
        validate_object(content, "image/png", len(content), None)
