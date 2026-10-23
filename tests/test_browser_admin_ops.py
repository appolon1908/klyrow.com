from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import delete

from apps.gateway.app.platform import app
from apps.gateway.app.browser_admin_ops import (
    AbuseAlertStateIn,
    AdminDunningIn,
    _billing_configuration,
    _decode_audit_cursor,
    _encode_audit_cursor,
    _require_platform_admin,
    browser_admin_abuse,
    browser_admin_abuse_alert_state,
    browser_admin_abuse_evaluate,
    browser_admin_billing_dunning,
    router,
)
from apps.gateway.app.delivery_controls import AbuseAlert, AbuseIn, ResourceSuspension
from apps.gateway.app.main import Audit, Base, DB, Tenant, User, engine


EXPECTED_ROUTES = {
    ("/app/api/admin/abuse", "GET"),
    ("/app/api/admin/abuse/evaluate", "POST"),
    ("/app/api/admin/abuse/suspensions", "POST"),
    ("/app/api/admin/abuse/suspensions/{item_id}/release", "POST"),
    ("/app/api/admin/abuse/alerts/{alert_id}/state", "POST"),
    ("/app/api/admin/reconciliation", "GET"),
    ("/app/api/admin/reconciliation", "POST"),
    ("/app/api/admin/reconciliation/billing", "GET"),
    ("/app/api/admin/reconciliation/{run_id}", "GET"),
    ("/app/api/admin/billing/overview", "GET"),
    ("/app/api/admin/billing/subscriptions", "GET"),
    ("/app/api/admin/billing/dunning", "POST"),
    ("/app/api/admin/audit", "GET"),
}


def _routes(source):
    return {
        (route.path, method)
        for route in source
        if isinstance(route, APIRoute)
        for method in route.methods or ()
        if route.path.startswith("/app/api/admin/")
    }


def test_admin_browser_surface_is_exact_and_registered():
    assert _routes(router.routes) == EXPECTED_ROUTES
    effective = {
        (path, method)
        for path, method in _routes(app.routes)
        if (path, method) in EXPECTED_ROUTES
    }
    assert effective == EXPECTED_ROUTES


def test_every_admin_browser_mutation_requires_csrf():
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods or set()
        if methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
            dependency_names = {
                getattr(dependency.call, "__name__", "")
                for dependency in route.dependant.dependencies
            }
            assert "admin_csrf_guard" in dependency_names, route.path


def test_every_admin_browser_success_response_has_typed_json_schema():
    schema = app.openapi()
    for path, method in EXPECTED_ROUTES:
        operation = schema["paths"][path][method.lower()]
        status = "201" if method == "POST" and path in {
            "/app/api/admin/abuse/evaluate",
            "/app/api/admin/abuse/suspensions",
            "/app/api/admin/reconciliation",
        } else "200"
        response_schema = (
            operation["responses"][status]["content"]["application/json"]["schema"]
        )
        assert response_schema, (method, path)
        assert response_schema != {}, (method, path)


def test_every_admin_browser_route_requires_browser_context():
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        dependency_names = {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }
        assert "admin_browser_context" in dependency_names, route.path


def test_platform_admin_db_authority_is_required():
    class FakeSession:
        def __init__(self, user):
            self.user = user

        def get(self, _model, _identifier):
            return self.user

    ctx = {"sub": "owner"}
    assert _require_platform_admin(
        ctx, FakeSession(SimpleNamespace(enabled=True, role="platform_admin"))
    ) == ctx
    for user in (
        None,
        SimpleNamespace(enabled=False, role="platform_admin"),
        SimpleNamespace(enabled=True, role="tenant_admin"),
    ):
        with pytest.raises(HTTPException) as exc:
            _require_platform_admin(ctx, FakeSession(user))
        assert exc.value.status_code == 403
        assert exc.value.detail == "platform_admin_required"


def test_audit_cursor_round_trip_is_stable_and_opaque():
    item = Audit(
        id="audit-123",
        tenant_id="tenant-a",
        actor="operator-a",
        action="billing.reconciled",
        created_at=datetime(2026, 9, 21, 19, 45, tzinfo=timezone.utc),
    )
    cursor = _encode_audit_cursor(item)
    assert "audit-123" not in cursor
    created_at, item_id = _decode_audit_cursor(cursor)
    assert created_at == item.created_at
    assert item_id == item.id
    with pytest.raises(HTTPException) as exc:
        _decode_audit_cursor("not-a-valid-cursor")
    assert exc.value.status_code == 422
    assert exc.value.detail == "invalid_audit_cursor"


def test_billing_configuration_exposes_capability_state_not_secrets(monkeypatch):
    status = SimpleNamespace(providers={"stripe": "sandbox", "paypal": "disabled"})
    settings = SimpleNamespace(
        enabled=True,
        live_charging_enabled=False,
        dunning_enabled=True,
        refunds_enabled=True,
        reconciliation_enabled=True,
        capability_status=lambda: status,
    )
    monkeypatch.setattr(
        "apps.gateway.app.browser_admin_ops.load_billing_settings", lambda: settings
    )
    result = _billing_configuration()
    assert result == {
        "valid": True,
        "error": None,
        "enabled": True,
        "live_charging_enabled": False,
        "dunning_enabled": True,
        "refunds_enabled": True,
        "reconciliation_enabled": True,
        "providers": {"stripe": "sandbox", "paypal": "disabled"},
    }
    serialized = str(result).lower()
    assert "secret" not in serialized
    assert "password" not in serialized


def test_abuse_evaluation_reuses_canonical_authority_and_is_readable():
    Base.metadata.create_all(engine)
    suffix = uuid.uuid4().hex
    tenant_id = f"tenant-admin-abuse-{suffix}"
    user_id = f"platform-admin-{suffix}"
    ctx = {"sub": user_id, "tenant": tenant_id, "role": "platform_admin"}

    with DB() as session:
        session.add(Tenant(id=tenant_id, name="Admin Abuse Test", enabled=True, quota=100))
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                email=f"{suffix}@example.invalid",
                password_hash="not-used",
                role="platform_admin",
                enabled=True,
            )
        )
        session.commit()

        result = browser_admin_abuse_evaluate(
            AbuseIn(
                tenant_id=tenant_id,
                bounce_rate=0.02,
                complaint_rate=0.02,
                invalid_rate=0.01,
                volume_ratio=1,
            ),
            ctx=ctx,
            _browser_session=None,
            session=session,
        )
        assert result["state"] == "SUSPENDED"
        assert result["suspended"] is True
        assert session.get(Tenant, tenant_id).enabled is False

        state = browser_admin_abuse(
            state="OPEN",
            tenant_id=tenant_id,
            limit=10,
            ctx=ctx,
            session=session,
        )
        assert len(state["alerts"]) == 1
        assert state["alerts"][0]["tenant_id"] == tenant_id
        assert state["alerts"][0]["severity"] == "CRITICAL"
        alert_id = state["alerts"][0]["id"]

        acknowledged = browser_admin_abuse_alert_state(
            alert_id,
            AbuseAlertStateIn(state="ACKNOWLEDGED"),
            ctx=ctx,
            _browser_session=None,
            session=session,
        )
        assert acknowledged["state"] == "ACKNOWLEDGED"
        resolved = browser_admin_abuse_alert_state(
            alert_id,
            AbuseAlertStateIn(state="RESOLVED"),
            ctx=ctx,
            _browser_session=None,
            session=session,
        )
        assert resolved["state"] == "RESOLVED"
        with pytest.raises(HTTPException) as transition:
            browser_admin_abuse_alert_state(
                alert_id,
                AbuseAlertStateIn(state="ACKNOWLEDGED"),
                ctx=ctx,
                _browser_session=None,
                session=session,
            )
        assert transition.value.status_code == 409

        session.execute(delete(Audit).where(Audit.tenant_id == tenant_id))
        session.execute(delete(ResourceSuspension).where(ResourceSuspension.tenant_id == tenant_id))
        session.execute(delete(AbuseAlert).where(AbuseAlert.tenant_id == tenant_id))
        session.execute(delete(User).where(User.id == user_id))
        session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        session.commit()


def test_admin_dunning_is_fail_closed_when_capability_is_disabled(monkeypatch):
    class FakeSession:
        def get(self, _model, _identifier):
            return SimpleNamespace(enabled=True, role="platform_admin")

    monkeypatch.setattr(
        "apps.gateway.app.browser_admin_ops.load_billing_settings",
        lambda: SimpleNamespace(enabled=False, dunning_enabled=False),
    )
    with pytest.raises(HTTPException) as invalid:
        browser_admin_billing_dunning(
            AdminDunningIn(grace_days=21, suspend_days=7),
            ctx={"sub": "owner"},
            _browser_session=None,
            session=FakeSession(),
        )
    assert invalid.value.status_code == 422
    assert invalid.value.detail == "billing_dunning_schedule_invalid"

    with pytest.raises(HTTPException) as exc:
        browser_admin_billing_dunning(
            AdminDunningIn(grace_days=7, suspend_days=21),
            ctx={"sub": "owner"},
            _browser_session=None,
            session=FakeSession(),
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == "billing_dunning_disabled"
