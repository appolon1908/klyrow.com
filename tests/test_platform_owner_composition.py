from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request

from apps.gateway.app import platform_owner
from apps.gateway.app.auth_bff import SESSION_COOKIE
from apps.gateway.app.platform_owner_policy import PlatformOwnerError


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def scalar(self, _statement):
        return SimpleNamespace(revoked_at=None)


class _LockingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.statements = []

    def scalar(self, statement):
        self.statements.append(statement)
        return self.responses.pop(0)


def _runtime_app(monkeypatch, validator):
    monkeypatch.setattr(platform_owner, "DB", lambda: _FakeSession())
    monkeypatch.setattr(platform_owner, "_validate_session", validator)

    app = FastAPI()
    app.state.instrumented_responses = []

    @app.middleware("http")
    async def instrumentation(request, call_next):
        response = await call_next(request)
        app.state.instrumented_responses.append((request.url.path, response.status_code))
        response.headers.update(
            {
                "X-Test-Instrumented": "true",
                "X-Request-Id": "test-request-id",
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    @app.post("/app/api/credits")
    def protected_operation():
        return {"handler": "ran"}

    @app.get("/app/api/admin/test")
    def privileged_operation():
        return {"handler": "admin-ran"}

    @app.get("/health/live")
    def public_health():
        return {"status": "ok"}

    platform_owner.install_platform_owner_guard(app)
    # These tests exercise the outer defense-in-depth middleware. Focused tests
    # below invoke the request-scoped role-stability dependency directly.
    app.dependency_overrides[platform_owner.platform_owner_role_stability_guard] = lambda: None
    return app


def _request(path: str = "/app/api/admin/test") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("app.klyrow.com", 443),
            "state": {},
        }
    )


def _authority_snapshot(*, role: str = "platform_admin"):
    item = SimpleNamespace(
        id="browser-session",
        user_id="owner-user",
        tenant_id="owner-tenant",
        identity_id="owner-identity",
        role="OWNER",
        revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    user = SimpleNamespace(
        id="owner-user",
        role=role,
        enabled=True,
    )
    member = SimpleNamespace(
        user_id="owner-user",
        tenant_id="owner-tenant",
        role="OWNER",
        active=True,
    )
    identity = SimpleNamespace(
        id="owner-identity",
        user_id="owner-user",
        issuer="https://auth.codestra.co/realms/codestra",
        subject="owner-subject",
        enabled=True,
    )
    context = {
        "sid": item.id,
        "sub": item.user_id,
        "tenant": item.tenant_id,
        "identity_id": item.identity_id,
    }
    return item, user, member, identity, context


def test_production_composition_installs_exact_owner_guard() -> None:
    source = Path("apps/gateway/app/platform.py").read_text(encoding="utf-8")
    assert "install_platform_owner_guard(app)" in source
    assert "platform_owner_router" in source


def test_guard_covers_every_browser_api_for_platform_admin_sessions() -> None:
    source = Path("apps/gateway/app/platform_owner.py").read_text(encoding="utf-8")
    assert 'PLATFORM_OWNER_PATH_PREFIXES = ("/app/api/",)' in source
    assert "PlatformOwnerConfig.from_env" in source
    assert "validate_platform_owner_claims" in source


def test_every_browser_route_gets_same_session_role_stability_dependency(
    monkeypatch,
) -> None:
    app = _runtime_app(monkeypatch, lambda _session, _item: None)
    browser_routes = [
        route
        for route in app.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/app/api/")
    ]
    assert sorted(route.path for route in browser_routes) == [
        "/app/api/admin/test",
        "/app/api/credits",
    ]
    for route in browser_routes:
        assert route.dependant.dependencies
        assert (
            route.dependant.dependencies[0].call
            is platform_owner.platform_owner_role_stability_guard
        )

    public_route = next(
        route
        for route in app.router.routes
        if isinstance(route, APIRoute) and route.path == "/health/live"
    )
    assert all(
        dependency.call is not platform_owner.platform_owner_role_stability_guard
        for dependency in public_route.dependant.dependencies
    )
    assert app.state.klyrow_platform_owner_browser_routes == (
        "/app/api/admin/test",
        "/app/api/credits",
    )


def test_locked_browser_authority_uses_four_shared_refreshing_locks() -> None:
    item, user, member, identity, context = _authority_snapshot()
    session = _LockingSession(item, user, member, identity)

    assert platform_owner._locked_browser_authority(session, context) == (
        item,
        user,
        member,
        identity,
    )
    assert len(session.statements) == 4
    assert all(statement._for_update_arg is not None for statement in session.statements)
    assert all(statement._for_update_arg.read is True for statement in session.statements)
    assert all(
        statement.get_execution_options().get("populate_existing") is True
        for statement in session.statements
    )


def test_workspace_switch_uses_exclusive_session_and_identity_locks() -> None:
    item, user, member, identity, context = _authority_snapshot()
    session = _LockingSession(item, user, member, identity)

    platform_owner._locked_browser_authority(
        session,
        context,
        exclusive_session=True,
        exclusive_identity=True,
    )

    assert [statement._for_update_arg.read for statement in session.statements] == [
        False,
        True,
        True,
        False,
    ]


def test_workspace_switch_route_selects_exclusive_lock_modes(monkeypatch) -> None:
    item, user, member, identity, context = _authority_snapshot(role="tenant_admin")
    observed = []

    def locked(_session, _context, **lock_modes):
        observed.append(lock_modes)
        return item, user, member, identity

    monkeypatch.setattr(platform_owner, "_locked_browser_authority", locked)
    monkeypatch.setattr(
        platform_owner,
        "_validate_owner_objects",
        lambda *_args, **_kwargs: False,
    )
    request = _request("/app/api/organizations/tenant-b/switch")

    platform_owner.platform_owner_role_stability_guard(
        request,
        ctx=context,
        s=object(),
    )

    assert observed == [{"exclusive_session": True, "exclusive_identity": True}]
    assert request.state.klyrow_platform_owner_authority_locked is True


def test_normal_browser_role_is_locked_without_owner_configuration(
    monkeypatch,
) -> None:
    item, user, member, identity, context = _authority_snapshot(role="tenant_admin")
    session = _LockingSession(item, user, member, identity)
    observed = []

    def validate(*values, require_platform_admin):
        observed.append((values, require_platform_admin))
        return False

    monkeypatch.setattr(platform_owner, "_validate_owner_objects", validate)
    request = _request("/app/api/credits")
    platform_owner.platform_owner_role_stability_guard(
        request,
        ctx=context,
        s=session,
    )

    assert observed == [
        ((item, user, member, identity), False),
    ]
    assert request.state.klyrow_platform_owner_authority_locked is True
    assert request.state.klyrow_platform_owner_validated is False
    assert request.state.klyrow_platform_owner_session_id == item.id
    assert context["role"] == member.role


def test_guard_refreshes_stale_handler_role_from_locked_membership() -> None:
    item, user, member, identity, context = _authority_snapshot(role="tenant_admin")
    item.role = "MEMBER"
    member.role = "MEMBER"
    context["role"] = "platform_admin"
    session = _LockingSession(item, user, member, identity)
    request = _request("/app/api/mailboxes/mailbox-1")

    platform_owner.platform_owner_role_stability_guard(
        request,
        ctx=context,
        s=session,
    )

    assert context["role"] == "MEMBER"
    assert request.state.klyrow_platform_owner_authority_locked is True
    assert request.state.klyrow_platform_owner_validated is False


def test_normal_role_still_requires_current_base_authority() -> None:
    item, user, _member, identity, _context = _authority_snapshot(role="tenant_admin")
    item.role = "MEMBER"

    with pytest.raises(PlatformOwnerError) as denied:
        platform_owner._validate_owner_objects(
            item,
            user,
            None,
            identity,
            require_platform_admin=False,
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == "platform_owner_membership_mismatch"


def test_admin_route_validates_locked_snapshot_and_marks_request(
    monkeypatch,
) -> None:
    item, user, member, identity, context = _authority_snapshot()
    session = _LockingSession(item, user, member, identity)
    observed = []

    def validate(*values, require_platform_admin):
        observed.append((values, require_platform_admin))
        return True

    monkeypatch.setattr(platform_owner, "_validate_owner_objects", validate)
    request = _request()
    platform_owner.platform_owner_role_stability_guard(
        request,
        ctx=context,
        s=session,
    )

    assert observed == [
        ((item, user, member, identity), True),
    ]
    assert request.state.klyrow_platform_owner_authority_locked is True
    assert request.state.klyrow_platform_owner_validated is True
    assert request.state.klyrow_platform_owner_session_id == item.id
    assert context["role"] == member.role


def test_non_admin_route_still_validates_a_current_platform_role(
    monkeypatch,
) -> None:
    item, user, member, identity, context = _authority_snapshot()
    session = _LockingSession(item, user, member, identity)
    observed = []

    def validate(*values, require_platform_admin):
        observed.append((values, require_platform_admin))
        return True

    monkeypatch.setattr(platform_owner, "_validate_owner_objects", validate)
    request = _request("/app/api/mailboxes/mailbox-1")
    platform_owner.platform_owner_role_stability_guard(
        request,
        ctx=context,
        s=session,
    )

    assert observed == [
        ((item, user, member, identity), False),
    ]
    assert request.state.klyrow_platform_owner_authority_locked is True
    assert request.state.klyrow_platform_owner_validated is True
    assert context["role"] == member.role


def test_admin_guard_denies_non_platform_role_before_handler() -> None:
    item, user, member, identity, context = _authority_snapshot(role="tenant_admin")
    session = _LockingSession(item, user, member, identity)

    with pytest.raises(HTTPException) as denied:
        platform_owner.platform_owner_role_stability_guard(
            _request(),
            ctx=context,
            s=session,
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == "platform_admin_required"
    assert denied.value.headers == {"Cache-Control": "no-store"}


def test_same_transaction_lock_contract_is_explicit_in_source() -> None:
    source = Path("apps/gateway/app/platform_owner.py").read_text(encoding="utf-8")
    assert source.count(".with_for_update(") == 4
    assert ".with_for_update(read=not exclusive_session)" in source
    assert ".with_for_update(read=not exclusive_identity)" in source
    assert source.count(".execution_options(populate_existing=True)") >= 4
    assert "WORKSPACE_SWITCH_ROUTE" in source
    assert "platform_owner_role_stability_guard" in source
    assert "_install_browser_route_dependencies" in source
    assert "route.dependant.dependencies.insert" in source
    assert "request.state.klyrow_platform_owner_authority_locked = True" in source


def test_unbound_step_up_redirect_is_hidden_and_fails_closed() -> None:
    source = Path("apps/gateway/app/platform_owner.py").read_text(encoding="utf-8")
    assert '@router.get("/auth/step-up", include_in_schema=False)' in source
    assert "platform_owner_step_up_flow_binding_required" in source
    assert "_authorization_url(" not in source

    with pytest.raises(HTTPException) as denied:
        platform_owner.platform_owner_step_up(_ctx={"sub": "owner-candidate"})

    assert denied.value.status_code == 503
    assert denied.value.detail == "platform_owner_step_up_flow_binding_required"
    assert denied.value.headers == {"Cache-Control": "no-store"}


def test_runtime_guard_blocks_before_a_browser_api_handler(monkeypatch) -> None:
    def deny(_session, _item):
        raise PlatformOwnerError(403, "platform_owner_identity_mismatch")

    app = _runtime_app(monkeypatch, deny)
    with TestClient(app) as client:
        response = client.post(
            "/app/api/credits",
            headers={"Cookie": f"{SESSION_COOKIE}=opaque-session"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "platform_owner_identity_mismatch"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-test-instrumented"] == "true"
    assert response.headers["x-request-id"] == "test-request-id"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert app.state.instrumented_responses == [("/app/api/credits", 403)]


def test_runtime_guard_fails_closed_on_missing_owner_configuration(
    monkeypatch,
) -> None:
    def unavailable(_session, _item):
        raise PlatformOwnerError(503, "platform_owner_not_configured")

    app = _runtime_app(monkeypatch, unavailable)
    with TestClient(app) as client:
        response = client.post(
            "/app/api/credits",
            headers={"Cookie": f"{SESSION_COOKIE}=opaque-session"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "platform_owner_not_configured"}
    assert response.headers["x-test-instrumented"] == "true"
    assert app.state.instrumented_responses == [("/app/api/credits", 503)]


def test_owner_lookup_runs_through_threadpool(monkeypatch) -> None:
    offloaded = []

    async def fake_run_in_threadpool(function, *args):
        offloaded.append((function, args))
        return function(*args)

    monkeypatch.setattr(
        platform_owner,
        "run_in_threadpool",
        fake_run_in_threadpool,
    )
    app = _runtime_app(monkeypatch, lambda _session, _item: None)

    with TestClient(app) as client:
        response = client.post(
            "/app/api/credits",
            headers={"Cookie": f"{SESSION_COOKIE}=opaque-session"},
        )

    assert response.status_code == 200
    assert offloaded == [(platform_owner._validate_raw_session, ("opaque-session",))]


def test_runtime_guard_allows_a_validated_owner_to_reach_handler(
    monkeypatch,
) -> None:
    app = _runtime_app(monkeypatch, lambda _session, _item: None)
    with TestClient(app) as client:
        response = client.post(
            "/app/api/credits",
            headers={"Cookie": f"{SESSION_COOKIE}=opaque-session"},
        )

    assert response.status_code == 200
    assert response.json() == {"handler": "ran"}
    assert response.headers["x-test-instrumented"] == "true"


def test_runtime_guard_does_not_wrap_non_browser_api_routes(
    monkeypatch,
) -> None:
    def must_not_run(_session, _item):
        raise AssertionError("owner guard ran outside /app/api/")

    app = _runtime_app(monkeypatch, must_not_run)
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"Cookie": f"{SESSION_COOKIE}=opaque-session"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-test-instrumented"] == "true"


def test_guard_refuses_late_installation_after_stack_build() -> None:
    app = FastAPI()

    @app.get("/app/api/admin/test")
    def admin_test():
        return {"status": "ok"}

    @app.get("/")
    def root():
        return {"status": "ok"}

    with TestClient(app) as client:
        assert client.get("/").status_code == 200

    with pytest.raises(
        RuntimeError,
        match="before the ASGI stack is built",
    ):
        platform_owner.install_platform_owner_guard(app)
