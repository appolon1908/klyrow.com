from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from apps.gateway.app.main import Base, Tenant, User, sha
from apps.gateway.app import auth_bff, browser_security_fixes
from apps.gateway.app.auth_bff import BrowserSession
from apps.gateway.app.browser_step_up_identity import (
    resolve_existing_step_up_identity,
)
from apps.gateway.app.platform import app
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

ISSUER = "https://auth.codestra.co/realms/codestra"


@pytest.fixture
def step_up_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        tenant = Tenant(
            id="tenant-step-up",
            name="Step-up Tenant",
            quota=100,
            enabled=True,
        )
        user = User(
            id="user-step-up",
            tenant_id=tenant.id,
            email="owner@example.com",
            password_hash="not-used",
            role="tenant_admin",
            enabled=True,
        )
        identity = OidcIdentity(
            id="identity-step-up",
            issuer=ISSUER,
            subject="subject-step-up",
            user_id=user.id,
            default_tenant_id=tenant.id,
            identity_type="KLYROW_ONLY",
            enabled=True,
        )
        membership = TenantMember(
            id="membership-step-up",
            tenant_id=tenant.id,
            user_id=user.id,
            role="OWNER",
            active=True,
        )
        browser = BrowserSession(
            id="browser-step-up",
            token_hash=sha("browser-step-up-token"),
            csrf_hash=sha("browser-step-up-csrf"),
            identity_id=identity.id,
            user_id=user.id,
            tenant_id=tenant.id,
            role=membership.role,
            expires_at=auth_bff.now() + timedelta(minutes=10),
        )
        session.add_all([tenant, user, identity, membership, browser])
        session.commit()
        yield session
    engine.dispose()


def _transaction(mode: str = "step-up:browser-step-up"):
    return SimpleNamespace(mode=mode)


def test_canonical_composition_installs_exact_existing_identity_resolver() -> None:
    assert app.state.klyrow_existing_step_up_identity_guard_installed is True
    assert browser_security_fixes._step_up_identity_context is resolve_existing_step_up_identity


def test_exact_existing_identity_and_membership_are_returned(
    step_up_session,
) -> None:
    old_session, identity, user, membership = resolve_existing_step_up_identity(
        step_up_session,
        _transaction(),
        {"sub": "subject-step-up"},
    )
    assert old_session.id == "browser-step-up"
    assert identity.id == "identity-step-up"
    assert user.id == "user-step-up"
    assert membership.id == "membership-step-up"


def test_different_subject_fails_without_first_login_or_provisioning(
    step_up_session,
    monkeypatch,
) -> None:
    before_users = step_up_session.scalar(select(func.count()).select_from(User))
    before_tenants = step_up_session.scalar(select(func.count()).select_from(Tenant))

    def provisioning_must_not_run(*args, **kwargs):
        pytest.fail("step-up entered first-login provisioning")

    monkeypatch.setattr(
        auth_bff,
        "_identity_context",
        provisioning_must_not_run,
    )
    with pytest.raises(HTTPException) as denied:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {"sub": "different-keycloak-subject"},
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == "step_up_identity_mismatch"
    assert step_up_session.scalar(select(func.count()).select_from(User)) == before_users
    assert step_up_session.scalar(select(func.count()).select_from(Tenant)) == before_tenants


def test_missing_subject_fails_closed(step_up_session) -> None:
    with pytest.raises(HTTPException) as denied:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {},
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "step_up_identity_mismatch"


def test_disabled_identity_fails_closed(step_up_session) -> None:
    identity = step_up_session.get(OidcIdentity, "identity-step-up")
    identity.enabled = False
    step_up_session.commit()

    with pytest.raises(HTTPException) as denied:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {"sub": "subject-step-up"},
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "step_up_identity_mismatch"


def test_disabled_user_fails_closed(step_up_session) -> None:
    user = step_up_session.get(User, "user-step-up")
    user.enabled = False
    step_up_session.commit()

    with pytest.raises(HTTPException) as denied:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {"sub": "subject-step-up"},
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "step_up_account_disabled"


def test_inactive_membership_fails_closed(step_up_session) -> None:
    membership = step_up_session.get(TenantMember, "membership-step-up")
    membership.active = False
    step_up_session.commit()

    with pytest.raises(HTTPException) as denied:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {"sub": "subject-step-up"},
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "step_up_workspace_invalid"


def test_suspended_tenant_fails_closed(step_up_session) -> None:
    tenant = step_up_session.get(Tenant, "tenant-step-up")
    tenant.enabled = False
    step_up_session.commit()

    with pytest.raises(HTTPException) as denied:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {"sub": "subject-step-up"},
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "step_up_workspace_invalid"


def test_revoked_or_expired_session_fails_closed(step_up_session) -> None:
    browser = step_up_session.get(BrowserSession, "browser-step-up")
    browser.revoked_at = auth_bff.now()
    step_up_session.commit()
    with pytest.raises(HTTPException) as revoked:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {"sub": "subject-step-up"},
        )
    assert revoked.value.status_code == 401
    assert revoked.value.detail == "step_up_session_invalid"

    browser.revoked_at = None
    browser.expires_at = auth_bff.now() - timedelta(seconds=1)
    step_up_session.commit()
    with pytest.raises(HTTPException) as expired:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction(),
            {"sub": "subject-step-up"},
        )
    assert expired.value.status_code == 401
    assert expired.value.detail == "step_up_session_invalid"


def test_non_step_up_transaction_mode_fails_closed(step_up_session) -> None:
    with pytest.raises(HTTPException) as denied:
        resolve_existing_step_up_identity(
            step_up_session,
            _transaction("login"),
            {"sub": "subject-step-up"},
        )
    assert denied.value.status_code == 401
    assert denied.value.detail == "step_up_session_invalid"
