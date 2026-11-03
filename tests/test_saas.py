import pytest

pytestmark = pytest.mark.usefixtures("canonical_api_owner")

import os

os.environ.update(
    KLYROW_DATABASE_URL="sqlite:///./saas-test.db",
    KLYROW_SESSION_SECRET="test-session-secret-at-least-32-bytes",
    KLYROW_WEBHOOK_SECRET="hook-secret",
    KLYROW_SAFE_MODE="true",
    KLYROW_ADMIN_EMAIL="admin@example.com",
    KLYROW_ADMIN_PASSWORD="correct-horse-battery-staple",
    KLYROW_AI_ENABLED="false",
    KLYROW_RATE_PER_MINUTE="1000",
    KLYROW_AUTH_RATE_PER_MINUTE="1000",
)
from fastapi.testclient import TestClient

from apps.gateway.app.main import (
    DB,
    AllowedSender,
    Base,
    Domain,
    Tenant,
    User,
    app,
    engine,
    ph,
    rate_buckets,
)
from apps.gateway.app.saas import ExperimentAssignment, MfaConfig, totp

client = TestClient(app)


def setup_module():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        for n, role in (("a", "tenant_admin"), ("b", "tenant_admin"), ("root", "platform_admin")):
            s.add(Tenant(id=n, name=n, quota=100))
            s.add(
                User(
                    id=n,
                    tenant_id=n,
                    email=f"{n}@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role=role,
                )
            )
            s.add(Domain(id=n, tenant_id=n, domain=f"{n}.example.com", token=n, verified=True))
            s.add(
                AllowedSender(id=n, tenant_id=n, address=f"sender@{n}.example.com", role="support")
            )
        s.commit()


def login(n, otp=None):
    body = {"email": f"{n}@example.com", "password": "long-enough-password"}
    if otp:
        body["otp"] = otp
    r = client.post("/v1/auth/login", json=body)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def hdr(n="a", otp=None):
    return {"Authorization": "Bearer " + login(n, otp)}


def profile(email="person@example.net", tenant="a", attributes=None):
    r = client.post(
        "/v1/profiles",
        headers=hdr(tenant),
        json={
            "email": email,
            "external_id": "crm-" + tenant + email,
            "attributes": attributes or {"country": "DE", "score": 12},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_profiles_events_identity_and_tenant_isolation():
    pid = profile()
    assert client.get("/v1/profiles/" + pid, headers=hdr("b")).status_code == 404
    merged = client.post(
        "/v1/profiles",
        headers=hdr(),
        json={
            "email": "person@example.net",
            "customer_id": "customer-1",
            "attributes": {"tier": "pro"},
        },
    )
    assert merged.status_code == 201 and merged.json()["id"] == pid
    e = client.post(
        "/v1/events",
        headers=hdr(),
        json={"profile_id": pid, "name": "purchase", "properties": {"amount": 25}},
    )
    assert e.status_code == 202
    assert len(client.get(f"/v1/profiles/{pid}/timeline", headers=hdr()).json()) == 1


def test_dynamic_nested_segment_and_suppression_awareness():
    pid = profile("segment@example.net", attributes={"country": "DE", "score": 20})
    client.post("/v1/events", headers=hdr(), json={"profile_id": pid, "name": "purchase"})
    rules = {
        "all": [
            {"field": "country", "op": "eq", "value": "DE"},
            {
                "any": [
                    {"field": "score", "op": "gte", "value": 10},
                    {"field": "event", "name": "purchase", "op": "gte", "count": 1},
                ]
            },
        ]
    }
    seg = client.post("/v1/segments", headers=hdr(), json={"name": "Engaged DE", "rules": rules})
    assert seg.status_code == 201
    preview = client.get(f"/v1/segments/{seg.json()['id']}/preview", headers=hdr()).json()
    assert preview["estimated_size"] >= 1 and any(x["id"] == pid for x in preview["sample"])
    assert (
        client.get(f"/v1/segments/{seg.json()['id']}/preview", headers=hdr("b")).status_code == 404
    )


def test_consent_preferences_and_stream_separation():
    pid = profile("consent@example.net")
    base = {
        "to": "consent@example.net",
        "sender": "sender@a.example.com",
        "subject": "Hi",
        "html": "<p>Hi</p>",
        "stream": "marketing",
    }
    assert (
        client.post(
            "/v1/email/send",
            headers={**hdr(), "Idempotency-Key": "marketing-no-consent"},
            json=base,
        ).status_code
        == 422
    )
    grant = client.post(
        "/v1/consents",
        headers=hdr(),
        json={
            "profile_id": pid,
            "topic": "marketing",
            "status": "granted",
            "source": "signup_form",
            "version": "2026-08",
        },
    )
    assert grant.status_code == 201
    sent = client.post(
        "/v1/email/send", headers={**hdr(), "Idempotency-Key": "marketing-consented"}, json=base
    )
    assert (
        sent.status_code == 202
        and sent.json()["safe_mode"]
        and sent.json()["stream"] == "marketing"
    )
    pref = client.put(
        f"/v1/profiles/{pid}/preferences/marketing",
        headers=hdr(),
        json={"topic": "marketing", "subscribed": False},
    )
    assert pref.status_code == 200
    assert (
        client.post(
            "/v1/email/send",
            headers={**hdr(), "Idempotency-Key": "marketing-unsubscribed"},
            json=base,
        ).status_code
        == 422
    )
    tx = {**base, "stream": "transactional"}
    assert (
        client.post(
            "/v1/email/send", headers={**hdr(), "Idempotency-Key": "transactional"}, json=tx
        ).status_code
        == 202
    )


def test_journey_publish_pause_resume_and_conversion_goal():
    pid = profile("journey@example.net")
    graph = {
        "nodes": [{"id": "start", "type": "trigger"}, {"id": "goal", "type": "goal"}],
        "edges": [{"from": "start", "to": "goal"}],
    }
    j = client.post(
        "/v1/journeys",
        headers=hdr(),
        json={"name": "Purchase journey", "graph": graph, "goal_event": "converted"},
    )
    assert j.status_code == 201
    jid = j.json()["id"]
    assert client.post(f"/v1/journeys/{jid}/publish", headers=hdr()).json()["status"] == "active"
    run = client.post(f"/v1/journeys/{jid}/runs", headers=hdr(), json={"profile_id": pid})
    assert run.status_code == 201
    assert client.post(f"/v1/journeys/{jid}/pause", headers=hdr()).json()["status"] == "paused"
    assert client.post(f"/v1/journeys/{jid}/resume", headers=hdr()).json()["status"] == "active"
    client.post("/v1/events", headers=hdr(), json={"profile_id": pid, "name": "converted"})
    runs = client.get(f"/v1/journeys/{jid}/runs", headers=hdr()).json()
    assert runs[0]["converted"] and runs[0]["status"] == "completed"


def test_analytics_onboarding_audit_and_openapi():
    overview = client.get("/v1/analytics/overview", headers=hdr())
    assert overview.status_code == 200 and overview.json()["profiles"] >= 1
    onboarding = client.put(
        "/v1/onboarding",
        headers=hdr(),
        json={
            "step": 12,
            "use_case": "transactional",
            "checklist": {"domain": True, "consent": True},
        },
    )
    assert (
        onboarding.status_code == 200
        and onboarding.json()["production_gate"] is False
        and onboarding.json()["completed"] is False
    )
    assert client.get("/v1/audit", headers=hdr()).status_code == 200
    spec = client.get("/v1/developer/openapi.json")
    assert spec.status_code == 200 and "/v1/profiles" in spec.json()["paths"]


def test_personalization_sanitization_rate_limit_and_request_id():
    h = hdr()
    rendered = client.post(
        "/v1/content/render",
        headers=h,
        json={"html": "<h1>Hello {{name}}</h1>", "variables": {"name": "<Admin>"}},
    )
    assert rendered.status_code == 200 and "&lt;Admin&gt;" in rendered.json()["html"]
    assert (
        client.post(
            "/v1/content/render",
            headers=h,
            json={"html": "<script>alert(1)</script>", "variables": {}},
        ).status_code
        == 422
    )
    os.environ["KLYROW_RATE_PER_MINUTE"] = "2"
    rate_buckets.clear()
    assert client.get("/v1/me", headers=h).status_code == 200
    assert client.get("/v1/me", headers=h).status_code == 200
    r = client.get("/v1/me", headers=h)
    assert r.status_code == 429 and r.headers.get("X-Request-Id")
    os.environ["KLYROW_RATE_PER_MINUTE"] = "1000"
    rate_buckets.clear()


def test_mfa_and_session_revocation():
    h = hdr()
    setup = client.post("/v1/auth/mfa/setup", headers=h).json()
    enable = client.post("/v1/auth/mfa/enable", headers=h, json={"code": totp(setup["secret"])})
    assert enable.status_code == 200 and len(enable.json()["recovery_codes"]) == 8
    denied = client.post(
        "/v1/auth/login", json={"email": "a@example.com", "password": "long-enough-password"}
    )
    assert denied.status_code == 401 and denied.json()["detail"] == "mfa_required"
    token = login("a", totp(setup["secret"]))
    mh = {"Authorization": "Bearer " + token}
    sessions = client.get("/v1/auth/sessions", headers=mh).json()
    sid = sessions[-1]["id"]
    assert client.delete("/v1/auth/sessions/" + sid, headers=mh).status_code == 204
    assert client.get("/v1/me", headers=mh).status_code == 401


def test_experiment_ai_billing_and_admin_suspension():
    secret = None
    with DB() as s:
        secret = s.get(MfaConfig, "a").secret
    ah = hdr("a", totp(secret))
    created = client.post(
        "/v1/profiles",
        headers=ah,
        json={"email": "experiment@example.net", "external_id": "crm-experiment"},
    )
    assert created.status_code == 201
    pid = created.json()["id"]
    exp = client.post(
        "/v1/experiments",
        headers=ah,
        json={
            "name": "Subject",
            "variants": [{"name": "A"}, {"name": "B"}],
            "metric": "conversion",
        },
    ).json()
    v1 = client.post(f"/v1/experiments/{exp['id']}/assign/{pid}", headers=ah).json()["variant"]
    v2 = client.post(f"/v1/experiments/{exp['id']}/assign/{pid}", headers=ah).json()["variant"]
    assert v1 == v2
    with DB() as s:
        a = s.scalar(
            __import__("sqlalchemy")
            .select(ExperimentAssignment)
            .where(ExperimentAssignment.experiment_id == exp["id"])
        )
        a.converted = True
        s.commit()
    assert client.get(f"/v1/experiments/{exp['id']}/results", headers=ah).json()["winner"] == v1
    assert (
        client.post(
            "/v1/ai/assist", headers=ah, json={"capability": "subject", "prompt": "Draft a subject"}
        ).status_code
        == 503
    )
    assert client.get("/v1/billing/usage", headers=ah).json()["billing_active"] is True
    root = hdr("root")
    assert client.get("/v1/admin/operations", headers=root).status_code == 200
    assert client.post("/v1/admin/tenants/b/suspend", headers=root).status_code == 200
    assert client.get("/v1/domains", headers=hdr("b")).status_code == 403
