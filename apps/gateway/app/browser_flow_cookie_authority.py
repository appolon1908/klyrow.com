"""State-specific host-only OIDC flow-cookie authority.

New browser flows use one authoritative cookie name per server-side state
transaction. An older callback can therefore clear only its own binding and
cannot overwrite or expire a newer tab's binding. During the bounded rollout,
a capped historical shared-cookie mirror is also emitted for compatibility with
pre-existing clients and tests; callbacks for new flows never delete that
mirror, and exact per-flow cookies always take precedence.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

from . import auth_bff
from . import browser_security_fixes as legacy

FLOW_COOKIE_PREFIX = "__Host-klyrow_oidc_flow-"
FLOW_COOKIE_PATH = "/"

# Capture rollout-compatible implementations before installation mutates the
# module globals used by the already-created FastAPI endpoint functions.
_LEGACY_SET_FLOW_COOKIE = legacy.set_flow_cookie
_LEGACY_REQUIRE_FLOW_COOKIE = legacy.require_flow_cookie
_LEGACY_REMOVE_FLOW_COOKIE = legacy.remove_flow_cookie
_INSTALLED = False


def flow_cookie_name(state: str) -> str:
    """Return a host-only cookie name that does not expose the OIDC state."""

    normalized = str(state or "")
    if not normalized:
        raise ValueError("oidc_state_required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return FLOW_COOKIE_PREFIX + digest


def set_flow_cookie(response, request: Request, state: str) -> None:
    """Emit an authoritative per-flow binding and bounded rollout mirror."""

    # The mirror is capped by the historical implementation, expires with the
    # same short flow TTL, and remains browser-bound. It is non-authoritative
    # whenever the exact per-flow cookie is present. Keeping it during rollout
    # lets transactions started across an application update complete safely.
    _LEGACY_SET_FLOW_COOKIE(response, request, state)
    response.set_cookie(
        flow_cookie_name(state),
        legacy.flow_binding(state),
        max_age=auth_bff._flow_ttl(),
        secure=True,
        httponly=True,
        samesite="lax",
        path=FLOW_COOKIE_PATH,
    )


def require_flow_cookie(request: Request, state: str) -> None:
    """Require the exact per-flow cookie, with bounded legacy fallback."""

    expected = legacy.flow_binding(state)
    supplied = str(request.cookies.get(flow_cookie_name(state), ""))
    if supplied:
        if hmac.compare_digest(supplied, expected):
            return
        # A present but incorrect exact cookie is never rescued by the broader
        # compatibility mirror.
        raise HTTPException(
            403,
            "oidc_flow_cookie_mismatch",
            headers={"Cache-Control": "no-store"},
        )

    try:
        _LEGACY_REQUIRE_FLOW_COOKIE(request, state)
    except HTTPException as exc:
        raise HTTPException(
            403,
            "oidc_flow_cookie_mismatch",
            headers={"Cache-Control": "no-store"},
        ) from exc


def remove_flow_cookie(response, request: Request, state: str) -> None:
    """Clear only the callback's own authoritative binding.

    A response generated from an older request never writes another flow's
    cookie name, so response reordering cannot invalidate a newer tab. The
    shared mirror is left to its bounded TTL for new flows. A pre-rollout
    request that has no exact cookie still uses the historical removal path.
    """

    name = flow_cookie_name(state)
    if name in request.cookies:
        response.delete_cookie(
            name,
            secure=True,
            httponly=True,
            samesite="lax",
            path=FLOW_COOKIE_PATH,
        )
        return
    _LEGACY_REMOVE_FLOW_COOKIE(response, request, state)


def install_per_flow_cookie_authority(app) -> None:
    """Install per-flow cookie functions into the canonical browser module."""

    global _INSTALLED
    if _INSTALLED or getattr(
        app.state,
        "klyrow_per_flow_cookie_authority_installed",
        False,
    ):
        return

    legacy.set_flow_cookie = set_flow_cookie
    legacy.require_flow_cookie = require_flow_cookie
    legacy.remove_flow_cookie = remove_flow_cookie
    app.state.klyrow_per_flow_cookie_authority_installed = True
    _INSTALLED = True


__all__ = [
    "FLOW_COOKIE_PATH",
    "FLOW_COOKIE_PREFIX",
    "flow_cookie_name",
    "install_per_flow_cookie_authority",
    "remove_flow_cookie",
    "require_flow_cookie",
    "set_flow_cookie",
]
