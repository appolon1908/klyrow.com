from pathlib import Path

from fastapi.testclient import TestClient

from apps.gateway.app.platform import app


ROOT = Path(__file__).parents[1]
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
client = TestClient(app, base_url="https://app.klyrow.test")


def _openapi_methods():
    schema = app.openapi()
    return {
        (path, method.upper())
        for path, operations in schema.get("paths", {}).items()
        for method in operations
        if method.lower() in HTTP_METHODS
    }


def test_required_oidc_routes_are_visible_in_runtime_route_inventory():
    paths = {getattr(route, "path", "") for route in app.routes}
    assert {"/auth/login", "/auth/callback"} <= paths


def test_browser_session_routes_resolve_only_to_cookie_session_handlers():
    expected = {
        "/auth/sessions": "list_user_sessions",
        "/auth/sessions/{session_id}": "revoke_user_session",
    }
    resolved = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path not in expected:
            continue
        endpoint = getattr(route, "endpoint", None)
        assert endpoint.__module__ == "apps.gateway.app.browser_auth_actions"
        assert endpoint.__name__ == expected[path]
        assert path not in resolved
        resolved[path] = endpoint.__name__
    assert resolved == expected


def test_browser_api_contract_has_expected_methods():
    expected = {
        ("/auth/login", "GET"),
        ("/auth/signup", "GET"),
        ("/auth/google", "GET"),
        ("/auth/callback", "GET"),
        ("/auth/session", "GET"),
        ("/auth/refresh", "POST"),
        ("/auth/logout", "POST"),
        ("/auth/logout-all", "POST"),
        ("/auth/sessions", "GET"),
        ("/auth/sessions/{session_id}", "DELETE"),
        ("/app/api/context", "GET"),
        ("/app/api/dashboard", "GET"),
        ("/app/api/messages", "GET"),
        ("/app/api/email/send", "POST"),
        ("/app/api/onboarding", "GET"),
        ("/app/api/onboarding", "PATCH"),
        ("/app/api/onboarding/complete", "POST"),
        ("/app/api/organizations/{tenant_id}/switch", "POST"),
        ("/app/api/team", "GET"),
        ("/app/api/team/invitations", "POST"),
        ("/app/api/support/tickets", "GET"),
        ("/app/api/support/tickets", "POST"),
        ("/app/api/support/tickets/{ticket_id}", "GET"),
        ("/app/api/support/tickets/{ticket_id}/messages", "POST"),
        ("/app/api/admin/dashboard", "GET"),
        ("/app/api/domains", "GET"),
        ("/app/api/domains", "POST"),
        ("/app/api/domains/{item_id}/verify", "POST"),
        ("/app/api/senders", "GET"),
        ("/app/api/senders", "POST"),
        ("/app/api/provisioning/postal", "GET"),
        ("/app/api/provisioning/postal", "POST"),
        ("/app/api/provisioning/postal/retry", "POST"),
        ("/app/api/admin/provisioning/postal", "GET"),
        ("/app/api/admin/provisioning/postal/{tenant_id}/retry", "POST"),
        ("/app/api/media", "GET"),
        ("/app/api/media/uploads", "POST"),
        ("/app/api/media/uploads/{upload_reference}", "PUT"),
        ("/app/api/media/{asset_id}/complete", "POST"),
        ("/app/api/media/{asset_id}", "GET"),
        ("/app/api/media/{asset_id}/events", "GET"),
        ("/app/api/media/{asset_id}/archive", "POST"),
        ("/app/api/media/{asset_id}", "DELETE"),
    }
    actual = _openapi_methods()
    assert expected <= actual


def test_spa_fallback_does_not_shadow_browser_api_routes():
    # Runtime matching is the contract that matters. These endpoints all require
    # a browser session, so an anonymous request must reach the API auth boundary
    # (401) instead of the SPA shell (503 application_ui_not_built in CI).
    for path in (
        "/app/api/dashboard",
        "/app/api/domains",
        "/app/api/senders",
        "/app/api/provisioning/postal",
    ):
        response = client.get(path)
        assert response.status_code == 401, (path, response.status_code, response.text)
        assert response.headers.get("content-type", "").startswith("application/json")
        assert response.json().get("detail") != "application_ui_not_built"


def test_frontend_only_calls_registered_browser_api_paths():
    source = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "apps/web/src/api.ts",
            "apps/web/src/Dashboard.vue",
            "apps/web/src/Onboarding.vue",
            "apps/web/src/AdminDashboard.vue",
            "apps/web/src/Provisioning.vue",
        )
    )
    required = {
        "/auth/session",
        "/auth/logout",
        "/app/api/dashboard",
        "/app/api/team",
        "/app/api/email/send",
        "/app/api/onboarding",
        "/app/api/onboarding/complete",
        "/app/api/admin/dashboard",
        "/app/api/provisioning/postal",
        "/app/api/provisioning/postal/retry",
        "/app/api/admin/provisioning/postal",
    }
    for path in required:
        assert path in source, path


def test_frontend_has_no_browser_token_storage_contract():
    source = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "apps/web/src/api.ts",
            "apps/web/src/Dashboard.vue",
            "apps/web/src/Onboarding.vue",
            "apps/web/src/AdminDashboard.vue",
            "apps/web/src/Provisioning.vue",
        )
    )
    forbidden = ("localStorage", "sessionStorage", "refresh_token", "access_token", "id_token")
    for marker in forbidden:
        assert marker not in source
