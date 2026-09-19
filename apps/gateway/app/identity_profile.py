"""Closed identity authorities for hardened production and isolated staging."""
import os
from fastapi import HTTPException

PROFILES = {
    "production": ("https://auth.codestra.co/realms/codestra", "https://app.klyrow.com"),
    "staging": ("https://auth.codestra.co/realms/codestra", "https://staging.klyrow.com"),
}


def identity_authority() -> tuple[str, str]:
    profile = os.getenv("KLYROW_IDENTITY_PROFILE", "production")
    if profile not in PROFILES:
        raise HTTPException(503, "identity_profile_misconfigured")
    return PROFILES[profile]


def canonical_issuer() -> str:
    expected, _ = identity_authority()
    configured = os.getenv("KLYROW_OIDC_ISSUER", expected).rstrip("/")
    if configured != expected:
        raise HTTPException(503, "canonical_oidc_misconfigured")
    return configured
