"""M1-06: shadowed /auth routes are removed and route uniqueness is enforced."""

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff, browser_security_fixes
from apps.gateway.app.routing_guards import (
    assert_unique_api_routes,
    duplicate_api_routes,
    endpoint_label,
)


def _api_routes(routes, path: str, method: str) -> list[APIRoute]:
    return [
        route
        for route in routes
        if isinstance(route, APIRoute) and route.path == path and method in (route.methods or ())
    ]


def test_platform_app_has_no_duplicate_path_method():
    assert duplicate_api_routes(app.router.routes) == {}
    assert_unique_api_routes(app.router.routes)


def test_exactly_one_hardened_auth_callback_route():
    found = _api_routes(app.router.routes, "/auth/callback", "GET")
    assert len(found) == 1
    assert found[0].endpoint is browser_security_fixes.oidc_callback
    assert endpoint_label(found[0]) == "apps.gateway.app.browser_security_fixes:oidc_callback"
    # The un-hardened handler is no longer present on its source router either.
    assert _api_routes(auth_bff.router.routes, "/auth/callback", "GET") == []
    assert _api_routes(browser_security_fixes.browser_router.routes, "/auth/callback", "GET")


def test_security_installer_removes_callback_from_auth_bff_router_itself():
    """The removal must not depend on browser_auth_actions.install_auth_extensions."""
    assert _api_routes(auth_bff.router.routes, "/auth/callback", "GET") == []
    auth_bff.router.add_api_route("/auth/callback", auth_bff.oidc_callback, methods=["GET"])
    previous_installed = browser_security_fixes._INSTALLED
    browser_security_fixes._INSTALLED = False
    try:
        browser_security_fixes.install_browser_security_fixes(FastAPI())
        assert _api_routes(auth_bff.router.routes, "/auth/callback", "GET") == []
    finally:
        browser_security_fixes._INSTALLED = previous_installed
        browser_security_fixes._remove_route(
            auth_bff.router.routes, path="/auth/callback", method="GET"
        )


def test_duplicate_guard_raises_regardless_of_registration_order():
    """Reverse order: the un-hardened callback registered *after* the hardened
    one would previously resolve first silently; now composition refuses it."""
    throwaway = FastAPI()
    throwaway.router.routes.extend(browser_security_fixes.browser_router.routes)
    throwaway.router.routes.extend(auth_bff.router.routes)
    assert_unique_api_routes(throwaway.router.routes)

    reversed_app = FastAPI()
    reversed_app.add_api_route("/auth/callback", auth_bff.oidc_callback, methods=["GET"])
    reversed_app.router.routes.extend(browser_security_fixes.browser_router.routes)
    with pytest.raises(RuntimeError, match=r"^duplicate_route: GET /auth/callback -> ") as info:
        assert_unique_api_routes(reversed_app.router.routes)
    message = str(info.value)
    assert "apps.gateway.app.auth_bff:oidc_callback" in message
    assert "apps.gateway.app.browser_security_fixes:oidc_callback" in message
