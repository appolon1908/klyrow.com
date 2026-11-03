import os
from datetime import UTC, datetime, timedelta

os.environ.update(
    KLYROW_DATABASE_URL="sqlite:///./customer-event-test.db",
    KLYROW_SESSION_SECRET="test-session-secret-at-least-32-bytes",
    KLYROW_WEBHOOK_SECRET="hook-secret",
    KLYROW_SAFE_MODE="true",
    KLYROW_RATE_PER_MINUTE="1000",
    KLYROW_AUTH_RATE_PER_MINUTE="1000",
)

from fastapi.testclient import TestClient

from apps.gateway.app.main import DB, Base, Tenant, User, app, engine, ph

client = TestClient(app)


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        for tenant in (
            "event-tenant",
            "batch-tenant",
            "lookup-tenant",
            "other-event-tenant",
            "other",
        ):
            session.add(Tenant(id=tenant, name=tenant, quota=100))
            session.add(
                User(
                    id=tenant,
                    tenant_id=tenant,
                    email=f"{tenant}@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="tenant_admin",
                )
            )
        session.commit()


def headers(tenant: str = "event-tenant") -> dict[str, str]:
    response = client.post(
        "/v1/auth/login",
        json={"email": f"{tenant}@example.com", "password": "long-enough-password"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def profile(tenant: str = "event-tenant") -> str:
    response = client.post(
        "/v1/profiles",
        headers=headers(tenant),
        json={"external_id": f"crm-{tenant}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_event_idempotency_is_tenant_and_source_scoped():
    profile_id = profile()
    payload = {
        "profile_id": profile_id,
        "name": "checkout.completed",
        "source": "storefront",
        "idempotency_key": "checkout-42",
        "properties": {"order": "42"},
    }
    first = client.post("/v1/events", headers=headers(), json=payload)
    replay = client.post("/v1/events", headers=headers(), json=payload)
    assert first.status_code == replay.status_code == 202
    assert replay.json() == {"id": first.json()["id"], "accepted": True, "replayed": True}

    conflict = client.post(
        "/v1/events", headers=headers(), json={**payload, "name": "checkout.failed"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "event_idempotency_conflict"

    other_profile = profile("other-event-tenant")
    other = client.post(
        "/v1/events",
        headers=headers("other-event-tenant"),
        json={**payload, "profile_id": other_profile},
    )
    assert other.status_code == 202
    assert other.json()["id"] != first.json()["id"]


def test_batch_reports_partial_failures_without_losing_valid_events():
    profile_id = profile("batch-tenant")
    response = client.post(
        "/v1/events/batch",
        headers=headers("batch-tenant"),
        json={
            "events": [
                {"profile_id": profile_id, "name": "valid", "idempotency_key": "one"},
                {"profile_id": "foreign-profile", "name": "invalid"},
                {"profile_id": profile_id, "name": "also.valid", "idempotency_key": "two"},
            ]
        },
    )
    assert response.status_code == 207, response.text
    assert response.json()["accepted"] == 2
    assert response.json()["rejected"] == 1
    timeline = client.get(f"/v1/profiles/{profile_id}/timeline", headers=headers("batch-tenant"))
    assert {event["name"] for event in timeline.json()} == {"valid", "also.valid"}


def test_event_requires_timezone_and_lookup_is_tenant_scoped():
    profile_id = profile("lookup-tenant")
    naive = client.post(
        "/v1/events",
        headers=headers("lookup-tenant"),
        json={
            "profile_id": profile_id,
            "name": "bad.time",
            "occurred_at": datetime(2026, 1, 1).isoformat(),
        },
    )
    assert naive.status_code == 422
    assert naive.json()["detail"] == "occurred_at_timezone_required"

    found = client.get(
        "/v1/profiles/lookup",
        headers=headers("lookup-tenant"),
        params={"external_id": "crm-lookup-tenant"},
    )
    assert found.status_code == 200
    assert found.json()["id"] == profile_id
    assert (
        client.get(
            "/v1/profiles/lookup",
            headers=headers("other"),
            params={"external_id": "crm-lookup-tenant"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/profiles/lookup",
            headers=headers("lookup-tenant"),
            params={"external_id": "x", "phone": "y"},
        ).status_code
        == 422
    )


def test_header_replay_binds_complete_timestamp_payload():
    pid = profile()
    h = {**headers(), "Idempotency-Key": "header-timestamp"}
    payload = {"profile_id": pid, "name": "clock", "occurred_at": "2026-01-01T00:00:00Z"}
    first = client.post("/v1/events", headers=h, json=payload)
    assert first.status_code == 202, first.text
    replay = client.post(
        "/v1/events", headers=h, json={**payload, "occurred_at": "2025-12-31T19:00:00-05:00"}
    )
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == first.json()["id"]
    assert (
        client.post(
            "/v1/events", headers=h, json={**payload, "occurred_at": "2026-01-02T00:00:00Z"}
        ).status_code
        == 409
    )
    assert (
        client.post("/v1/events", headers=h, json={"profile_id": pid, "name": "clock"}).status_code
        == 409
    )
    assert (
        client.post(
            "/v1/events", headers=h, json={**payload, "idempotency_key": "different"}
        ).status_code
        == 422
    )


def test_batch_header_replays_items_and_preserves_partial_conflicts():
    pid = profile()
    h = {**headers(), "Idempotency-Key": "batch-header"}
    items = [
        {"profile_id": pid, "name": "batch.first"},
        {"profile_id": pid, "name": "batch.second"},
    ]
    first = client.post("/v1/events/batch", headers=h, json={"events": items})
    replay = client.post("/v1/events/batch", headers=h, json={"events": items})
    assert first.status_code == replay.status_code == 207
    assert all(r["replayed"] for r in replay.json()["results"])
    assert [r["id"] for r in first.json()["results"]] == [r["id"] for r in replay.json()["results"]]
    changed = client.post(
        "/v1/events/batch", headers=h, json={"events": [items[0], {**items[1], "name": "changed"}]}
    )
    assert changed.json()["accepted"] == changed.json()["rejected"] == 1
    assert changed.json()["results"][1]["code"] == "event_idempotency_conflict"


def test_event_openapi_describes_optional_header_and_non_atomic_batch():
    schema = app.openapi()
    for path, model in [
        ("/v1/events", "OPTIONAL_REQUEST_SCOPED"),
        ("/v1/events/batch", "OPTIONAL_ITEM_SCOPED_NON_ATOMIC"),
    ]:
        operation = schema["paths"][path]["post"]
        assert operation["x-idempotency-required"] is False
        assert operation["x-idempotency-model"] == model
        assert any(p["name"].lower() == "idempotency-key" for p in operation["parameters"])


def test_profile_list_is_cursor_paged_and_tenant_scoped():
    first = profile("lookup-tenant")
    second = client.post(
        "/v1/profiles", headers=headers("lookup-tenant"), json={"email": "second@example.net"}
    )
    assert second.status_code == 201
    page = client.get("/v1/profiles", headers=headers("lookup-tenant"), params={"limit": 1})
    assert page.status_code == 200
    assert len(page.json()["items"]) == 1
    assert page.json()["next_cursor"]
    next_page = client.get(
        "/v1/profiles",
        headers=headers("lookup-tenant"),
        params={"limit": 1, "cursor": page.json()["next_cursor"]},
    )
    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["id"] in {first, second.json()["id"]}
    assert (
        client.get("/v1/profiles", headers=headers("other"), params={"limit": 200}).json()["items"]
        == []
    )
    assert (
        client.get(
            "/v1/profiles", headers=headers("lookup-tenant"), params={"cursor": "not-a-cursor"}
        ).status_code
        == 422
    )


def test_secure_profile_import_export_jobs_and_idempotency():
    import_headers = {**headers("batch-tenant"), "Idempotency-Key": "import-job-1"}
    payload = {
        "object_reference": "b2://klyrow-imports/tenant/batch.csv",
        "object_sha256": "A" * 64,
        "row_count": 12,
    }
    first = client.post("/v1/profile-imports", headers=import_headers, json=payload)
    replay = client.post("/v1/profile-imports", headers=import_headers, json=payload)
    assert first.status_code == replay.status_code == 202
    assert replay.json()["duplicate"] is True and replay.json()["id"] == first.json()["id"]
    assert (
        client.post(
            "/v1/profile-imports",
            headers=import_headers,
            json={**payload, "object_sha256": "B" * 64},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/v1/profile-imports",
            headers=headers("batch-tenant"),
            json={**payload, "object_reference": "https://example.net/input.csv"},
        ).status_code
        == 422
    )

    export_headers = {**headers("batch-tenant"), "Idempotency-Key": "export-job-1"}
    export_payload = {"fields": ["id", "email", "attributes"], "filters": {"email": "example.net"}}
    export = client.post("/v1/profile-exports", headers=export_headers, json=export_payload)
    assert export.status_code == 202 and export.json()["state"] == "PENDING"
    assert (
        client.post("/v1/profile-exports", headers=export_headers, json=export_payload).json()[
            "duplicate"
        ]
        is True
    )
    assert (
        client.post(
            "/v1/profile-exports", headers=headers("batch-tenant"), json={"fields": ["password"]}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/v1/profile-imports/" + first.json()["id"], headers=headers("other")
        ).status_code
        == 404
    )


def test_retention_and_deletion_are_tenant_scoped_and_fail_closed():
    pid = profile("batch-tenant")
    defaults = client.get("/v1/customer-data/retention", headers=headers("batch-tenant"))
    assert defaults.status_code == 200 and defaults.json()["configured"] is False
    configured = client.put(
        "/v1/customer-data/retention",
        headers=headers("batch-tenant"),
        json={"profile_retention_days": 365, "event_retention_days": 180},
    )
    assert configured.status_code == 200 and configured.json()["profile_retention_days"] == 365
    scheduled_for = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    deletion = client.post(
        "/v1/customer-data/deletions",
        headers=headers("batch-tenant"),
        json={"profile_id": pid, "scheduled_for": scheduled_for, "reason": "customer request"},
    )
    assert deletion.status_code == 202 and deletion.json()["automatic_execution"] is False
    assert (
        client.post(
            "/v1/customer-data/deletions",
            headers=headers("batch-tenant"),
            json={"profile_id": pid, "scheduled_for": scheduled_for, "reason": "customer request"},
        ).json()["duplicate"]
        is True
    )
    assert (
        client.post(
            "/v1/customer-data/deletions/" + deletion.json()["id"] + "/cancel",
            headers=headers("other"),
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/customer-data/deletions/" + deletion.json()["id"] + "/cancel",
            headers=headers("batch-tenant"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/customer-data/deletions",
            headers=headers("batch-tenant"),
            json={"profile_id": pid, "scheduled_for": "2020-01-01T00:00:00Z", "reason": "too late"},
        ).status_code
        == 422
    )
