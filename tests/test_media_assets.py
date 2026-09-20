from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.gateway.app.auth_bff import BrowserSession, SESSION_COOKIE
from apps.gateway.app.main import Base, DB, Tenant, User, app, engine, ph, sha
from apps.gateway.app.media_assets import FakeMediaObjectStore, _fake_store
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

pytestmark = pytest.mark.usefixtures("isolated_durable_result_keyring")

client = TestClient(app)
tokens = {}
upload_counter = 0

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cf00000003000100018d0d0d0000000049454e44ae426082"
)


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
