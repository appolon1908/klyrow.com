"""Production ASGI composition root for Klyrow browser and API surfaces.

The browser SPA shell is composed here, never inside an API router. Starlette
uses first-match routing, so catch-all shell routes must be registered last.
"""
from fastapi import HTTPException
from fastapi.responses import FileResponse

from .main import AUTH_WEB_DIST, app
from . import auth_bff
from .auth_bff import router as auth_bff_router
from .browser_auth_actions import (
    install_auth_extensions,
    router as browser_auth_actions_router,
)
from .tenancy_onboarding import router as tenancy_onboarding_router
from .invitation_flow import (
    install_invitation_extensions,
    router as invitation_flow_router,
)
from .browser_email_setup import router as browser_email_setup_router
from .platform_owner import (
    install_platform_owner_guard,
    router as platform_owner_router,
)
from .postal_callback_attribution import (
    install_postal_callback_extension,
    router as postal_callback_router,
)
from .postal_provisioning import (
    resolve_identity_context_with_provisioning,
    router as postal_provisioning_router,
)
from .webmail import router as webmail_router
from .browser_security_fixes import (
    browser_router as browser_security_router,
    install_browser_security_fixes,
    legacy_router as legacy_security_router,
)
from .browser_flow_cookie_authority import (
    install_per_flow_cookie_authority,
)
from .browser_step_up_identity import (
    install_existing_step_up_identity_guard,
)
from .browser_step_up_deadline import (
    install_step_up_deadline_guard,
)

SHELL_PATHS = {"/app", "/onboarding", "/app/{path:path}"}

# Replace historical account/session/invitation/callback routes before route
# objects are copied into the production application.
install_auth_extensions()
install_invitation_extensions()
install_postal_callback_extension()
install_browser_security_fixes(app)
# New transactions use one host-only cookie per OIDC state so an older callback
# response cannot invalidate a newer tab. The installer retains bounded read
# compatibility for transactions started by the preceding release.
install_per_flow_cookie_authority(app)
# Step-up resolves only the initiating account and keeps parent revocation and
# child creation in one transaction. The deadline guard additionally prevents
# any rotated child from extending the parent session's absolute lifetime.
install_existing_step_up_identity_guard(app)
install_step_up_deadline_guard(app)

# The historical onboarding router contains SPA shell routes before its API
# routes. Strip those routes from the source router before registration.
for route in list(tenancy_onboarding_router.routes):
    if getattr(route, "path", "") in SHELL_PATHS:
        tenancy_onboarding_router.routes.remove(route)

# Remove legacy/product shell routes that may already exist on the core app.
for route in list(app.router.routes):
    if getattr(route, "path", "") in SHELL_PATHS or (
        getattr(route, "path", "") == "/admin"
        and getattr(route, "name", "") == "admin_portal"
    ):
        app.router.routes.remove(route)

if not getattr(app.state, "klyrow_postal_callback_route_registered", False):
    app.router.routes.extend(postal_callback_router.routes)
    app.state.klyrow_postal_callback_route_registered = True

# Register browser APIs once. These routers have no prefixes or router-level
# dependencies, so direct APIRoute registration keeps the runtime route table
# inspectable while preserving the original handlers and dependencies.
if not getattr(app.state, "klyrow_browser_api_routes_registered", False):
    auth_bff._identity_context = resolve_identity_context_with_provisioning
    for platform_router in (
        browser_security_router,
        legacy_security_router,
        auth_bff_router,
        platform_owner_router,
        browser_auth_actions_router,
        tenancy_onboarding_router,
        invitation_flow_router,
        browser_email_setup_router,
        postal_provisioning_router,
        webmail_router,
    ):
        app.router.routes.extend(platform_router.routes)
    app.state.klyrow_browser_api_routes_registered = True
else:
    auth_bff._identity_context = resolve_identity_context_with_provisioning

# Install after route registration but before the ASGI middleware stack is
# built. A platform-admin browser session then satisfies the exact-owner gate
# before any /app/api/ handler runs.
install_platform_owner_guard(app)

# Install cross-cutting runtime parity repairs only after every source module
# that captured the historical helpers has been imported.
from .runtime_authority_fixes import install_runtime_authority_fixes

install_runtime_authority_fixes()

from .usage_history import router as usage_history_router
from .account_api import router as account_router

if not getattr(app.state, "klyrow_usage_history_registered", False):
    app.include_router(usage_history_router)
    app.state.klyrow_usage_history_registered = True

if not getattr(app.state, "klyrow_account_registered", False):
    app.include_router(account_router)
    app.state.klyrow_account_registered = True

# The core app may have generated OpenAPI before browser composition.
app.openapi_schema = None


def _ui_index():
    index = AUTH_WEB_DIST / "index.html"
    if not index.exists():
        raise HTTPException(503, "application_ui_not_built")
    return FileResponse(
        index, media_type="text/html", headers={"Cache-Control": "no-store"}
    )


# Remove platform-owned shell routes too if this module is explicitly reloaded.
for route in list(app.router.routes):
    if getattr(route, "name", "") in {
        "platform_admin_ui",
        "product_app_ui",
    }:
        app.router.routes.remove(route)


@app.get("/admin", include_in_schema=False)
@app.get("/admin/{path:path}", include_in_schema=False)
def platform_admin_ui(path: str = ""):
    return _ui_index()


# Deliberately last: no /app/api/* route can ever be consumed by this catch-all.
@app.get("/app", include_in_schema=False)
@app.get("/onboarding", include_in_schema=False)
@app.get("/app/{path:path}", include_in_schema=False)
def product_app_ui(path: str = ""):
    return _ui_index()


from .openapi_authority import install_openapi_authority

install_openapi_authority(app)


__all__ = ["app"]
