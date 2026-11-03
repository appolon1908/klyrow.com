"""M1-05: the pre-Vue HTML portal/admin pages and their scripts are gone."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.gateway.app.main import app as core_app
from apps.gateway.app import platform

ROOT = Path(__file__).resolve().parents[1]
LEGACY_FILES = (
    "apps/gateway/app/portal.html",
    "apps/gateway/app/portal.js",
    "apps/gateway/app/admin.html",
    "apps/gateway/app/admin.js",
)
LEGACY_PATHS = ("/portal", "/admin", "/assets/portal.js", "/assets/admin.js")
LEGACY_HANDLERS = {"portal", "portal_js", "admin_portal", "admin_js"}


@pytest.mark.parametrize("relative", LEGACY_FILES)
def test_legacy_portal_files_do_not_exist(relative: str) -> None:
    assert not (ROOT / relative).exists(), relative


@pytest.mark.parametrize("path", ("/portal", "/assets/portal.js", "/assets/admin.js"))
def test_core_app_no_longer_serves_legacy_pages(path: str) -> None:
    response = TestClient(core_app).get(path)
    assert response.status_code == 404, (path, response.status_code)


def test_core_app_admin_is_never_the_legacy_html_page() -> None:
    # ``platform`` mutates the shared core app and registers the Vue admin
    # shell on /admin, so the only acceptable handlers are none (404) or that
    # shell (200, or 503 when the SPA bundle is absent). The inline legacy
    # page is gone either way.
    handlers = {
        getattr(route, "name", "")
        for route in core_app.routes
        if getattr(route, "path", "") == "/admin"
    }
    assert handlers <= {"platform_admin_ui"}
    response = TestClient(core_app).get("/admin")
    assert response.status_code in ((200, 503) if handlers else (404,))
    assert "/assets/admin.js" not in response.text


def test_core_app_has_no_legacy_handlers() -> None:
    names = {getattr(route, "name", "") for route in core_app.routes}
    assert names.isdisjoint(LEGACY_HANDLERS)
    paths = {getattr(route, "path", "") for route in core_app.routes}
    assert paths.isdisjoint({"/portal", "/assets/portal.js", "/assets/admin.js"})


def test_platform_app_serves_only_vue_shell_for_admin() -> None:
    handlers = {
        getattr(route, "name", "")
        for route in platform.app.routes
        if getattr(route, "path", "") == "/admin"
    }
    assert handlers == {"platform_admin_ui"}
    client = TestClient(platform.app)
    for path in ("/portal", "/assets/portal.js", "/assets/admin.js"):
        assert client.get(path).status_code == 404, path
    # /admin is the Vue admin shell (or 503 when the SPA bundle is absent in CI),
    # never the legacy inline HTML page.
    admin = client.get("/admin")
    assert admin.status_code in (200, 503)
    assert "/assets/admin.js" not in admin.text
