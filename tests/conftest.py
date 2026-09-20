from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_klyrow_public_origin(monkeypatch, request) -> None:
    """Prevent one test module's development origin from leaking suite-wide."""

    if request.module.__name__.endswith("test_browser_security_fixes"):
        monkeypatch.setenv(
            "KLYROW_PUBLIC_URL",
            "https://app.klyrow.test",
        )
    else:
        monkeypatch.delenv("KLYROW_PUBLIC_URL", raising=False)


@pytest.fixture(autouse=True)
def isolated_durable_result_keyring(tmp_path, monkeypatch):
    from apps.gateway.app.durable_keys import KEYRING_ENV, new_keyring_document
    path = tmp_path / "durable-result-keyring.json"
    path.write_text(new_keyring_document())
    path.chmod(0o600)
    monkeypatch.setenv(KEYRING_ENV, str(path))
    monkeypatch.setenv("KLYROW_DURABLE_RESULT_LEGACY_READ_ENABLED", "true")
    return path

@pytest.fixture
def canonical_api_owner(monkeypatch, request):
    """Issue real signed test OIDC tokens for existing owner business tests.

    Only the JWKS transport is replaced. Production signature, identity,
    membership, mailbox and MFA authorization all run unchanged.
    """
    import time
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from sqlalchemy import select

    from apps.gateway.app import main
    from apps.gateway.app.platform_owner_policy import CANONICAL_ISSUER
    from apps.gateway.app.tenancy import OidcIdentity, TenantMember

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = "8f52e865-d121-4c50-a41d-61c201402efd"
    monkeypatch.setenv("KLYROW_PLATFORM_OWNER_ISSUER", CANONICAL_ISSUER)
    monkeypatch.setenv("KLYROW_PLATFORM_OWNER_SUBJECT", subject)
    monkeypatch.setenv("KLYROW_PLATFORM_OWNER_EMAIL", "root@example.com")
    monkeypatch.setenv("KLYROW_PLATFORM_OWNER_STEP_UP_MAX_AGE_SECONDS", "300")
    monkeypatch.setenv("KLYROW_PLATFORM_OWNER_ACR_MFA_EVIDENCE_APPROVED", "false")
    monkeypatch.delenv("KLYROW_TENANT_RESOLVER_URL", raising=False)
    monkeypatch.setitem(
        main._jwks_clients, CANONICAL_ISSUER,
        SimpleNamespace(get_signing_key_from_jwt=lambda _raw: SimpleNamespace(key=key.public_key())),
    )

    def headers(**claim_updates):
        with main.DB() as session:
            user = session.get(main.User, "root")
            assert user is not None, "owner business fixture requires its existing root user"
            identity = session.scalar(
                select(OidcIdentity).where(
                    OidcIdentity.issuer == CANONICAL_ISSUER,
                    OidcIdentity.subject == subject,
                )
            )
            if identity is None:
                session.add(OidcIdentity(
                    id="owner-api-fixture-identity", issuer=CANONICAL_ISSUER,
                    subject=subject, user_id=user.id, default_tenant_id=user.tenant_id,
                    identity_type="HUMAN", enabled=True,
                ))
            member = session.scalar(select(TenantMember).where(
                TenantMember.tenant_id == user.tenant_id, TenantMember.user_id == user.id,
            ))
            if member is None:
                session.add(TenantMember(
                    id="owner-api-fixture-member", tenant_id=user.tenant_id,
                    user_id=user.id, role="platform_admin", active=True,
                ))
            session.commit()
        now = int(time.time())
        claims = {
            "iss": CANONICAL_ISSUER, "sub": subject,
            "aud": main.os.getenv("KLYROW_OIDC_AUDIENCE", "klyrow-api"),
            "iat": now, "exp": now + 300, "auth_time": now,
            "email": "root@example.com", "email_verified": True,
            "amr": ["pwd", "otp"],
        }
        claims.update(claim_updates)
        return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256")}

    # Migrate only explicitly marked business-test helpers; other callers and
    # the dedicated negative authorization tests retain their actual tokens.
    module = request.module
    helper_names = {
        "test_agent_mailboxes": ("hdr", None),
        "test_billing": ("login", "root@example.com"),
        "test_operations": ("h", None),
        "test_delivery_controls": ("h", None),
        "test_reseller": ("headers", None),
        "test_saas": ("hdr", "a"),
    }
    entry = helper_names.get(module.__name__.rsplit(".", 1)[-1])
    if entry:
        name, default_user = entry
        original = getattr(module, name)

        def owner_or_original(*args, **kwargs):
            user = args[0] if args else next(
                (kwargs[k] for k in ("email", "user", "n") if k in kwargs),
                default_user,
            )
            if user in {"root", "root@example.com"}:
                return headers()
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, owner_or_original)
    return headers
