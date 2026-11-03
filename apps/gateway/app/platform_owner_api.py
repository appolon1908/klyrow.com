"""Exact-owner authorization for bearer APIs and redacted authority readback."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import User, require
from .platform_owner_policy import (
    PlatformOwnerConfig,
    PlatformOwnerError,
    validate_platform_owner_claims,
)
from .tenancy import OidcIdentity, TenantMember

router = APIRouter(tags=["Platform owner security"])


def validate_api_owner(
    session: Session,
    context: dict,
    *,
    claims: dict | None,
    identity_id: str | None,
) -> None:
    """Validate only claims already signature-verified by the core authenticator.

    Resolver roles, tenant API keys and legacy local JWTs cannot supply this
    proof. Authority rows remain locked in the request's own transaction until
    its handler commits or rolls back. This function never assigns a role.
    """
    if claims is None or not identity_id:
        raise HTTPException(
            403,
            "platform_owner_oidc_required",
            headers={"Cache-Control": "no-store"},
        )
    if context.get("service") or context.get("api_key"):
        raise HTTPException(
            403,
            "platform_owner_human_required",
            headers={"Cache-Control": "no-store"},
        )
    try:
        user = session.scalar(
            select(User)
            .where(User.id == context["sub"])
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
        member = session.scalar(
            select(TenantMember)
            .where(
                TenantMember.tenant_id == context["tenant"],
                TenantMember.user_id == context["sub"],
            )
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
        identity = session.scalar(
            select(OidcIdentity)
            .where(OidcIdentity.id == identity_id)
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
        if not user or not user.enabled:
            raise PlatformOwnerError(403, "platform_owner_account_disabled")
        if str(user.role or "").lower() != "platform_admin":
            raise PlatformOwnerError(403, "platform_admin_required")
        if (
            not identity
            or not identity.enabled
            or identity.user_id != user.id
            or identity.subject != context.get("oidc_sub")
        ):
            raise PlatformOwnerError(403, "platform_owner_identity_mismatch")
        if str(identity.identity_type or "").upper() in {"SERVICE", "SERVICE_ACCOUNT"}:
            raise PlatformOwnerError(403, "platform_owner_human_required")
        if not member or not member.active or member.role != context.get("role"):
            raise PlatformOwnerError(403, "platform_owner_membership_mismatch")
        config = PlatformOwnerConfig.from_env()
        validate_platform_owner_claims(
            config,
            identity_issuer=identity.issuer,
            identity_subject=identity.subject,
            claims=claims,
            now_epoch=int(time.time()),
        )
    except PlatformOwnerError as exc:
        raise HTTPException(
            exc.status_code, exc.detail, headers={"Cache-Control": "no-store"}
        ) from exc


class OwnerAuthorityStatus(BaseModel):
    authority: Literal["PLATFORM_OWNER"] = "PLATFORM_OWNER"
    identity_binding: Literal["issuer_subject"] = "issuer_subject"
    mailbox_verified: Literal[True] = True
    mfa_verified: Literal[True] = True
    fresh_authentication_verified: Literal[True] = True
    authentication_max_age_seconds: int


def owner_authority_status(request: Request, response: Response, *, browser: bool = False):
    marker = "klyrow_platform_owner_validated" if browser else "klyrow_platform_owner_api_validated"
    if getattr(request.state, marker, False) is not True:
        raise HTTPException(
            403,
            "platform_owner_authority_required",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    return OwnerAuthorityStatus(
        authentication_max_age_seconds=PlatformOwnerConfig.from_env().step_up_max_age_seconds
    )


@router.get(
    "/v1/admin/security/platform-owner",
    response_model=OwnerAuthorityStatus,
    summary="Read the current caller's verified platform-owner authority",
    responses={
        401: {"description": "Missing or expired authentication"},
        403: {"description": "Owner identity, role, verified email or fresh MFA not satisfied"},
        503: {"description": "Protected owner configuration is incomplete or invalid"},
    },
)
def platform_owner_status(
    request: Request,
    response: Response,
    _context: dict = Depends(require("platform_admin")),
):
    """Readback only; does not enroll MFA, grant roles or authorize activation."""
    return owner_authority_status(request, response)


def resolve_api_owner_proof(session: Session, context: dict, raw: str):
    """Add local canonical OIDC proof after a successful resolver decision.

    Resolver permission and tenant checks have already succeeded. Its role is
    insufficient by itself: verify the same bearer token independently, then
    resolve the immutable identity to Klyrow's own user and membership.
    """
    import jwt
    from jwt import PyJWKClient

    from . import main
    from .platform_owner_policy import CANONICAL_ISSUER

    if str(context.get("identity_type") or "").upper() in {"SERVICE", "SERVICE_ACCOUNT"}:
        raise HTTPException(
            403, "platform_owner_human_required", headers={"Cache-Control": "no-store"}
        )
    try:
        if main.os.getenv("KLYROW_OIDC_ISSUER", CANONICAL_ISSUER) != CANONICAL_ISSUER:
            raise PlatformOwnerError(503, "platform_owner_issuer_misconfigured")
        client = main._jwks_clients.setdefault(
            CANONICAL_ISSUER,
            PyJWKClient(
                CANONICAL_ISSUER + "/protocol/openid-connect/certs", cache_keys=True, lifespan=300
            ),
        )
        # Reject local HMAC/API-key credentials before any JWKS request.
        if jwt.get_unverified_header(raw).get("alg") not in {"RS256", "ES256"}:
            raise ValueError("canonical_oidc_required")
        key = client.get_signing_key_from_jwt(raw)
        claims = jwt.decode(
            raw,
            key.key,
            algorithms=["RS256", "ES256"],
            issuer=CANONICAL_ISSUER,
            audience=main.os.getenv("KLYROW_OIDC_AUDIENCE", "klyrow-api"),
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except PlatformOwnerError as exc:
        raise HTTPException(
            exc.status_code, exc.detail, headers={"Cache-Control": "no-store"}
        ) from exc
    except Exception as exc:
        raise HTTPException(
            403, "platform_owner_oidc_required", headers={"Cache-Control": "no-store"}
        ) from exc

    identity = session.scalar(
        select(OidcIdentity).where(
            OidcIdentity.issuer == CANONICAL_ISSUER,
            OidcIdentity.subject == claims["sub"],
            OidcIdentity.enabled == True,
        )
    )
    if identity is None:
        raise HTTPException(
            403, "platform_owner_identity_mismatch", headers={"Cache-Control": "no-store"}
        )
    # This context is private to the request. Role and resolver permissions are
    # preserved; the locked local role/membership are checked by validate_api_owner.
    context.update(
        sub=identity.user_id,
        oidc_sub=identity.subject,
        identity_type=identity.identity_type,
        service=str(identity.identity_type).upper() in {"SERVICE", "SERVICE_ACCOUNT"},
    )
    return claims, identity.id
