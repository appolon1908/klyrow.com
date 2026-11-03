"""Browser integration for exact Klyrow platform-owner authorization."""

from __future__ import annotations

import time
from datetime import timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.dependencies.utils import get_parameterless_sub_dependant
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth_bff
from .auth_bff import BrowserSession, SESSION_COOKIE, browser_context
from .main import DB, User, db, sha
from .platform_owner_policy import (
    PlatformOwnerConfig,
    PlatformOwnerError,
    validate_platform_owner_claims,
)
from .tenancy import OidcIdentity, TenantMember
from .platform_owner_api import OwnerAuthorityStatus, owner_authority_status

router = APIRouter(tags=["Platform owner security"])
# The middleware is defense in depth for every browser API request made with a
# platform-admin session. Every browser API handler also receives a request-
# scoped dependency that locks the authority rows used by that handler's
# transaction. Normal users continue after the lock; any current platform role
# must satisfy the exact-owner policy.
PLATFORM_OWNER_PATH_PREFIXES = ("/app/api/",)
PLATFORM_OWNER_ADMIN_PREFIX = "/app/api/admin"
WORKSPACE_SWITCH_ROUTE = "/app/api/organizations/{tenant_id}/switch"


def _stored_claims(item: BrowserSession) -> dict:
    if not item.id_token_ciphertext:
        raise PlatformOwnerError(403, "platform_owner_reauthentication_required")
    try:
        raw = auth_bff._decrypt(item.id_token_ciphertext)
        claims = jwt.decode(
            raw,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
    except Exception as exc:
        raise PlatformOwnerError(
            403, "platform_owner_reauthentication_required"
        ) from exc
    if not isinstance(claims, dict):
        raise PlatformOwnerError(403, "platform_owner_claims_invalid")
    return claims


def _platform_role_present(
    session: BrowserSession, user: User | None, member: TenantMember | None
) -> bool:
    roles = {
        str(session.role or "").lower(),
        str(user.role if user else "").lower(),
        str(member.role if member else "").lower(),
    }
    return "platform_admin" in roles


def _validate_owner_objects(
    item: BrowserSession,
    user: User | None,
    member: TenantMember | None,
    identity: OidcIdentity | None,
    *,
    require_platform_admin: bool,
) -> bool:
    """Validate exact owner identity from one coherent authority snapshot."""

    user_is_platform_admin = bool(
        user and str(user.role or "").lower() == "platform_admin"
    )
    if not user or not user.enabled or user.id != item.user_id:
        raise PlatformOwnerError(403, "platform_owner_account_disabled")
    if (
        not member
        or not member.active
        or member.user_id != item.user_id
        or member.tenant_id != item.tenant_id
    ):
        raise PlatformOwnerError(403, "platform_owner_membership_mismatch")
    if (
        not identity
        or not identity.enabled
        or identity.id != item.identity_id
        or identity.user_id != item.user_id
    ):
        raise PlatformOwnerError(403, "platform_owner_identity_mismatch")
    if require_platform_admin and not user_is_platform_admin:
        raise PlatformOwnerError(403, "platform_admin_required")
    if not require_platform_admin and not _platform_role_present(item, user, member):
        return False
    try:
        canonical_issuer = auth_bff._canonical_issuer()
    except HTTPException as exc:
        raise PlatformOwnerError(
            503, "platform_owner_issuer_misconfigured"
        ) from exc
    config = PlatformOwnerConfig.from_env(canonical_issuer=canonical_issuer)
    validate_platform_owner_claims(
        config,
        identity_issuer=identity.issuer,
        identity_subject=identity.subject,
        claims=_stored_claims(item),
        now_epoch=int(time.time()),
    )
    return True


def _validate_session(s: Session, item: BrowserSession) -> None:
    """Defense-in-depth middleware validation outside the handler transaction."""

    user = s.get(User, item.user_id, populate_existing=True)
    member = s.scalar(
        select(TenantMember)
        .execution_options(populate_existing=True)
        .where(
            TenantMember.tenant_id == item.tenant_id,
            TenantMember.user_id == item.user_id,
            TenantMember.active == True,
        )
    )
    identity = s.get(OidcIdentity, item.identity_id, populate_existing=True)
    _validate_owner_objects(
        item,
        user,
        member,
        identity,
        require_platform_admin=False,
    )


def _validate_raw_session(raw: str) -> None:
    """Perform synchronous owner lookup and validation in one worker thread."""

    with DB() as s:
        item = s.scalar(
            select(BrowserSession).where(BrowserSession.token_hash == sha(raw))
        )
        if item and item.revoked_at is None:
            _validate_session(s, item)


def _locked_browser_authority(
    s: Session,
    ctx: dict,
    *,
    exclusive_session: bool = False,
    exclusive_identity: bool = False,
) -> tuple[BrowserSession, User | None, TenantMember | None, OidcIdentity | None]:
    """Lock browser authority rows in the handler's request-scoped transaction."""

    session_id = str(ctx.get("sid") or "")
    if not session_id:
        raise PlatformOwnerError(401, "authentication_required")
    item = s.scalar(
        select(BrowserSession)
        .execution_options(populate_existing=True)
        .where(BrowserSession.id == session_id)
        .with_for_update(read=not exclusive_session)
    )
    if not item or item.revoked_at is not None:
        raise PlatformOwnerError(401, "session_revoked")
    expiry = item.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= auth_bff.now():
        raise PlatformOwnerError(401, "session_expired")
    if (
        item.user_id != str(ctx.get("sub") or "")
        or item.tenant_id != str(ctx.get("tenant") or "")
        or item.identity_id != str(ctx.get("identity_id") or "")
    ):
        raise PlatformOwnerError(403, "platform_owner_session_mismatch")

    user = s.scalar(
        select(User)
        .execution_options(populate_existing=True)
        .where(User.id == item.user_id)
        .with_for_update(read=True)
    )
    member = s.scalar(
        select(TenantMember)
        .execution_options(populate_existing=True)
        .where(
            TenantMember.tenant_id == item.tenant_id,
            TenantMember.user_id == item.user_id,
            TenantMember.active == True,
        )
        .with_for_update(read=True)
    )
    identity = s.scalar(
        select(OidcIdentity)
        .execution_options(populate_existing=True)
        .where(OidcIdentity.id == item.identity_id)
        .with_for_update(read=not exclusive_identity)
    )
    return item, user, member, identity


def _is_browser_admin_path(path: str) -> bool:
    return path == PLATFORM_OWNER_ADMIN_PREFIX or path.startswith(
        PLATFORM_OWNER_ADMIN_PREFIX + "/"
    )


def _is_browser_api_path(path: str) -> bool:
    return path.startswith(PLATFORM_OWNER_PATH_PREFIXES)


def _is_workspace_switch_request(request: Request) -> bool:
    route = request.scope.get("route")
    route_path = str(getattr(route, "path", "") or "")
    if route_path:
        return route_path == WORKSPACE_SWITCH_ROUTE
    prefix = "/app/api/organizations/"
    suffix = "/switch"
    path = request.url.path
    if not path.startswith(prefix) or not path.endswith(suffix):
        return False
    tenant_id = path[len(prefix) : -len(suffix)]
    return bool(tenant_id) and "/" not in tenant_id


def platform_owner_role_stability_guard(
    request: Request,
    ctx: dict = Depends(browser_context),
    s: Session = Depends(db),
) -> None:
    """Lock role authority and validate exact ownership when platform-wide."""

    try:
        workspace_switch = _is_workspace_switch_request(request)
        item, user, member, identity = _locked_browser_authority(
            s,
            ctx,
            exclusive_session=workspace_switch,
            exclusive_identity=workspace_switch,
        )
        validated = _validate_owner_objects(
            item,
            user,
            member,
            identity,
            require_platform_admin=_is_browser_admin_path(request.url.path),
        )
        # browser_context is dependency-cached for the handler. Refresh its role
        # from the locked membership so a concurrent demotion cannot leave stale
        # platform authority in the handler-visible context.
        ctx["role"] = member.role
    except PlatformOwnerError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers={"Cache-Control": "no-store"},
        ) from exc
    request.state.klyrow_platform_owner_authority_locked = True
    request.state.klyrow_platform_owner_validated = validated
    request.state.klyrow_platform_owner_session_id = item.id


def _attach_platform_owner_dependency(route, path: str) -> None:
    """Attach the guard to an original or effective FastAPI route once."""

    if any(
        getattr(dependant, "call", None) is platform_owner_role_stability_guard
        for dependant in route.dependant.dependencies
    ):
        return
    marker = Depends(platform_owner_role_stability_guard)
    route.dependencies.insert(0, marker)
    route.dependant.dependencies.insert(
        0,
        get_parameterless_sub_dependant(
            depends=marker,
            path=path,
        ),
    )


def _install_browser_route_dependencies(app) -> tuple[str, ...]:
    """Attach the same-session role-stability guard to every browser API.

    FastAPI 0.141 keeps included routers as lazy route contexts. Walking only
    ``app.router.routes`` misses those effective routes, so protect both the
    current effective context and its original APIRoute for future rebuilds.
    """

    protected: list[str] = []
    for route_context in iter_route_contexts(app.routes):
        route = route_context.original_route
        path = str(route_context.path or "")
        if not isinstance(route, APIRoute) or not _is_browser_api_path(path):
            continue
        protected.append(path)
        _attach_platform_owner_dependency(route, route.path_format)
        _attach_platform_owner_dependency(
            route_context,
            str(route_context.path_format or path),
        )
    if not protected:
        raise RuntimeError("platform owner browser routes were not registered")
    result = tuple(sorted(set(protected)))
    app.state.klyrow_platform_owner_browser_routes = result
    return result


def install_platform_owner_guard(app) -> None:
    """Install transaction-bound route guards and outer defense-in-depth."""

    if app.middleware_stack is not None and not getattr(
        app.state, "klyrow_platform_owner_guard_installed", False
    ):
        raise RuntimeError(
            "platform owner guard must be installed before the ASGI stack is built"
        )
    _install_browser_route_dependencies(app)
    if getattr(app.state, "klyrow_platform_owner_guard_installed", False):
        return

    async def platform_owner_guard(request: Request, call_next):
        if request.url.path.startswith(PLATFORM_OWNER_PATH_PREFIXES):
            raw = request.cookies.get(SESSION_COOKIE, "")
            if raw:
                try:
                    await run_in_threadpool(_validate_raw_session, raw)
                except PlatformOwnerError as exc:
                    return JSONResponse(
                        {"detail": exc.detail},
                        status_code=exc.status_code,
                        headers={"Cache-Control": "no-store"},
                    )
        return await call_next(request)

    # Starlette inserts decorator/add_middleware registrations at the front,
    # making the latest registration outermost. Appending this middleware keeps
    # the existing request-ID, security-header, metric, and latency middleware
    # outside the owner guard, so even early denials receive full instrumentation.
    app.user_middleware.append(
        Middleware(BaseHTTPMiddleware, dispatch=platform_owner_guard)
    )
    app.state.klyrow_platform_owner_guard_installed = True


@router.get("/auth/step-up", include_in_schema=False)
def platform_owner_step_up(
    return_to: str = "/admin",
    _ctx: dict = Depends(browser_context),
):
    """Remain fail closed until the pre-session flow is browser-bound.

    Issue #83 owns the secure host-only flow-cookie prerequisite. Starting a
    fresh authorization transaction before that prerequisite lands would leave
    the step-up state vulnerable to login-CSRF/session-swap attacks. The route
    remains outside generated clients until that prerequisite is protected.
    """

    del return_to, _ctx
    raise HTTPException(
        status_code=503,
        detail="platform_owner_step_up_flow_binding_required",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/app/api/admin/security/platform-owner",
    response_model=OwnerAuthorityStatus,
    summary="Read the browser session's verified platform-owner authority",
    responses={
        401: {"description": "Missing or expired browser session"},
        403: {"description": "Exact owner, role, verified email or fresh MFA required"},
        503: {"description": "Protected owner configuration is incomplete or invalid"},
    },
)
def browser_platform_owner_status(
    request: Request,
    response: Response,
    _context: dict = Depends(browser_context),
):
    return owner_authority_status(request, response, browser=True)
