from fastapi.routing import APIRoute

from apps.gateway.app.platform import app
from apps.gateway.app.browser_profiles_suppressions import router


def _routes():
    return {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }


def test_m1a_browser_surface_is_tenant_scoped_and_complete():
    assert _routes() == {
        ("/app/api/profiles", "GET"),
        ("/app/api/profiles/{profile_id}", "GET"),
        ("/app/api/suppressions", "GET"),
        ("/app/api/suppressions", "POST"),
        ("/app/api/suppressions/{suppression_id}", "DELETE"),
    }
    effective = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
        if route.path.startswith("/app/api/profiles") or route.path.startswith("/app/api/suppressions")
    }
    assert effective == _routes()


def test_m1a_mutations_require_csrf_dependency():
    for route in router.routes:
        if not isinstance(route, APIRoute) or "GET" in (route.methods or ()):
            continue
        dependency_names = {dependency.call.__name__ for dependency in route.dependant.dependencies}
        assert "csrf_dependency" in dependency_names
