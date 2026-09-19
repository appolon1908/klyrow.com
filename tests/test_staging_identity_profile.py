import pytest
from fastapi import HTTPException
from apps.gateway.app.identity_profile import canonical_issuer, identity_authority
from apps.gateway.app import auth_bff


@pytest.mark.parametrize("profile,issuer,origin", [
    ("production", "https://auth.codestra.co/realms/codestra", "https://app.klyrow.com"),
    ("staging", "https://auth.codestra.co/realms/codestra", "https://staging.klyrow.com"),
])
def test_hardened_identity_profiles(monkeypatch, profile, issuer, origin):
    monkeypatch.setenv("KLYROW_ENV", "production")
    monkeypatch.setenv("KLYROW_IDENTITY_PROFILE", profile)
    monkeypatch.setenv("KLYROW_OIDC_ISSUER", issuer)
    monkeypatch.setenv("KLYROW_PUBLIC_URL", origin)
    assert canonical_issuer() == issuer
    assert auth_bff._canonical_issuer() == issuer
    assert auth_bff._public_origin() == origin
    monkeypatch.setenv("KLYROW_OIDC_ISSUER", "https://unregistered.invalid/realms/test")
    with pytest.raises(HTTPException):
        canonical_issuer()
    monkeypatch.setenv("KLYROW_PUBLIC_URL", "https://unregistered.invalid")
    with pytest.raises(HTTPException):
        auth_bff._public_origin()


def test_unknown_profile_rejected(monkeypatch):
    monkeypatch.setenv("KLYROW_IDENTITY_PROFILE", "unregistered")
    with pytest.raises(HTTPException):
        identity_authority()
