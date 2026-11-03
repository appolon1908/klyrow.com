"""Preserve the absolute browser-session deadline during owner step-up.

The canonical callback already locks and revokes the initiating session in the
same transaction that stages its replacement. This installer adds the missing
absolute-lifetime invariant: a rotated child may be shorter-lived, but it can
never outlive the locked parent session.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from .main import User
from . import auth_bff, browser_security_fixes
from .auth_bff import BrowserSession
from .tenancy import OidcIdentity, TenantMember

_ORIGINAL_STAGE_NEW_SESSION = browser_security_fixes._stage_new_session
_INSTALLED = False


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def stage_new_session_with_parent_deadline(
    session: Session,
    request: Request,
    identity: OidcIdentity,
    user: User,
    membership: TenantMember,
    tokens: dict,
    rotated_from_id: str | None = None,
):
    """Stage a replacement whose expiry is capped by its locked parent."""

    item, raw, csrf = _ORIGINAL_STAGE_NEW_SESSION(
        session,
        request,
        identity,
        user,
        membership,
        tokens,
        rotated_from_id=rotated_from_id,
    )
    if rotated_from_id is None:
        return item, raw, csrf

    # The step-up identity resolver selected this row with FOR UPDATE earlier
    # in the same request-scoped transaction. Session.get therefore returns the
    # same locked authority object without creating a second commit boundary.
    parent = session.get(BrowserSession, rotated_from_id)
    parent_expiry = _as_utc(parent.expires_at) if parent is not None else None
    issued_at = _as_utc(auth_bff.now())
    if (
        parent is None
        or parent.revoked_at is not None
        or parent_expiry is None
        or issued_at is None
        or parent_expiry <= issued_at
    ):
        raise HTTPException(
            401,
            "step_up_session_invalid",
            headers={"Cache-Control": "no-store"},
        )

    child_expiry = _as_utc(item.expires_at)
    if child_expiry is None or child_expiry > parent_expiry:
        item.expires_at = parent_expiry
    return item, raw, csrf


def install_step_up_deadline_guard(app) -> None:
    """Install once after browser-security route replacement is complete."""

    global _INSTALLED
    if _INSTALLED or getattr(
        app.state,
        "klyrow_step_up_deadline_guard_installed",
        False,
    ):
        return
    browser_security_fixes._stage_new_session = stage_new_session_with_parent_deadline
    app.state.klyrow_step_up_deadline_guard_installed = True
    _INSTALLED = True


__all__ = [
    "install_step_up_deadline_guard",
    "stage_new_session_with_parent_deadline",
]
