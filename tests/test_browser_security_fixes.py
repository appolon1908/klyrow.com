from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

ISSUER = "https://auth.codestra.co/realms/codestra"
os.environ.setdefault("KLYROW_OIDC_ISSUER", ISSUER)
os.environ.setdefault("KLYROW_PUBLIC_URL", "https://app.klyrow.test")

from apps.gateway.app.main import Base, MailIn, Tenant, User
from apps.gateway.app.platform import app
from apps.gateway.app import (
    auth_bff,
    tenancy_onboarding,
)
from apps.gateway.app import (
    browser_security_fixes as security,
)
from apps.gateway.app.auth_bff import BrowserSession, OidcLoginTransaction
from apps.gateway.app.browser_auth_actions import RecoveryRequest
from apps.gateway.app.browser_security_fixes import (
    FLOW_COOKIE,
    FLOW_COOKIE_MAX_BINDINGS,
    FLOW_COOKIE_PATH,
    accept_legacy_invitation,
    accept_selected_invitation,
    authorization_state,
    begin_recovery,
    browser_send,
    encode_flow_cookie,
    flow_binding,
    oidc_callback,
    parse_flow_cookie,
    platform_owner_step_up,
    set_flow_cookie,
)
from apps.gateway.app.tenancy import (
    AcceptIn,
    OidcIdentity,
    TenantInvitation,
    TenantMember,
)


@pytest.fixture
def isolated_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        session.add_all(
            [
                Tenant(
                    id="tenant-a",
                    name="Tenant A",
                    quota=100,
                    enabled=True,
                ),
                Tenant(
                    id="tenant-b",
                    name="Tenant B",
                    quota=100,
                    enabled=True,
                ),
                User(
                    id="user-a",
                    tenant_id="tenant-a",
                    email="member@example.com",
                    password_hash="not-used",
                    role="tenant_user",
                    enabled=True,
                ),
                OidcIdentity(
                    id="identity-a",
                    issuer=ISSUER,
                    subject="subject-a",
                    user_id="user-a",
                    default_tenant_id="tenant-a",
                    identity_type="KLYROW_ONLY",
                    enabled=True,
                ),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def _request(
    path: str,
    *,
    cookies: dict[str, str] | None = None,
    method: str = "GET",
) -> Request:
    headers = [(b"host", b"app.klyrow.test")]
    if cookies:
        headers.append(
            (
                b"cookie",
                "; ".join(f"{key}={value}" for key, value in cookies.items()).encode(),
            )
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("app.klyrow.test", 443),
            "state": {},
        }
    )


def _transaction(
    session,
    *,
    identifier: str,
    state: str,
    mode: str = "login",
    expired: bool = False,
) -> OidcLoginTransaction:
    item = OidcLoginTransaction(
        id=identifier,
        state_hash=auth_bff.sha(state),
        verifier_ciphertext=auth_bff._encrypt("verifier-" + identifier),
        nonce_ciphertext=auth_bff._encrypt("nonce-" + identifier),
        return_url="/app",
        mode=mode,
        expires_at=auth_bff.now() + timedelta(seconds=-1 if expired else 300),
    )
    session.add(item)
    session.commit()
    return item


def _token_success(monkeypatch, claims: dict | None = None) -> None:
    monkeypatch.setattr(
        auth_bff,
        "_exchange_code",
        lambda code, verifier, request: {
            "id_token": "id-token",
            "refresh_token": "refresh-token",
        },
    )
    monkeypatch.setattr(
        auth_bff,
        "_validate_id_token",
        lambda raw, expected_nonce=None: (
            claims
            or {
                "iss": ISSUER,
                "sub": "subject-a",
                "aud": "klyrow-portal",
            }
        ),
    )


def _successful_callback_dependencies(monkeypatch) -> None:
    _token_success(monkeypatch)
    monkeypatch.setattr(
        security,
        "_resolve_identity_context_uncommitted",
        lambda session, claims: (
            SimpleNamespace(id="identity-a"),
            SimpleNamespace(id="user-a"),
            SimpleNamespace(tenant_id="tenant-a", role="OWNER"),
        ),
    )
    monkeypatch.setattr(
        security,
        "_stage_postal_provisioning",
        lambda session, tenant_id: SimpleNamespace(tenant_id=tenant_id),
    )
    monkeypatch.setattr(
        security,
        "_stage_new_session",
        lambda *args, **kwargs: (
            SimpleNamespace(id="new-browser-session"),
            "opaque-browser-session",
            "stable-csrf",
        ),
    )


def _cookies(response) -> str:
    return "\n".join(
        value.decode() for name, value in response.raw_headers if name.lower() == b"set-cookie"
    )


def _cookie_line(response, name: str) -> str:
    return next(
        value.decode()
        for header, value in response.raw_headers
        if header.lower() == b"set-cookie" and value.decode().startswith(f"{name}=")
    )


def _cookie_value(response, name: str) -> str:
    cookie = SimpleCookie()
    cookie.load(_cookie_line(response, name))
    return cookie[name].value


def test_composition_replaces_each_security_sensitive_route_once() -> None:
    expected = {
        ("GET", "/auth/login"),
        ("GET", "/auth/signup"),
        ("GET", "/auth/google"),
        ("GET", "/auth/callback"),
        ("POST", "/auth/actions/recover"),
        ("POST", "/auth/actions/update-password"),
        ("POST", "/auth/actions/verify-email"),
        ("POST", "/auth/actions/invitation"),
        ("GET", "/auth/step-up"),
        ("POST", "/app/api/email/send"),
        ("POST", "/v1/team/invitations/accept"),
    }
    observed = {}
    for route in app.router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            key = (method, route.path)
            if key in expected:
                observed.setdefault(key, []).append(route)
    assert set(observed) == expected
    assert all(len(routes) == 1 for routes in observed.values())
    assert all(
        routes[0].endpoint.__module__.endswith("browser_security_fixes")
        for routes in observed.values()
    )
    assert app.state.klyrow_browser_security_fixes_installed is True


def test_flow_cookie_binding_is_domain_separated_and_state_specific() -> None:
    assert FLOW_COOKIE_PATH == "/"
    assert FLOW_COOKIE_MAX_BINDINGS == 8
    assert flow_binding("state-a") == flow_binding("state-a")
    assert flow_binding("state-a") != flow_binding("state-b")
    assert "state-a" not in flow_binding("state-a")
    assert parse_flow_cookie(flow_binding("state-a")) == [flow_binding("state-a")]
    encoded = encode_flow_cookie([flow_binding("state-a"), flow_binding("state-b")])
    assert parse_flow_cookie(encoded) == [
        flow_binding("state-a"),
        flow_binding("state-b"),
    ]
    url = "https://auth.example/authorize?state=state-a&client_id=klyrow"
    assert authorization_state(url) == "state-a"


def test_new_flow_preserves_at_most_eight_concurrent_tab_bindings() -> None:
    cookie_value = ""
    states = [f"tab-{index}" for index in range(10)]
    for state in states:
        response = Response()
        request = _request(
            "/auth/login",
            cookies={FLOW_COOKIE: cookie_value} if cookie_value else None,
        )
        set_flow_cookie(response, request, state)
        cookie_value = _cookie_value(response, FLOW_COOKIE)

    assert parse_flow_cookie(cookie_value) == [
        flow_binding(state) for state in states[-FLOW_COOKIE_MAX_BINDINGS:]
    ]
    line = _cookie_line(response, FLOW_COOKIE)
    assert "Secure" in line
    assert "HttpOnly" in line
    assert "SameSite=lax" in line
    assert "Path=/" in line
    assert "Domain=" not in line


def test_missing_or_wrong_cookie_never_consumes_state(
    isolated_session,
) -> None:
    state = "missing-cookie-state"
    transaction = _transaction(
        isolated_session,
        identifier="missing-cookie",
        state=state,
    )
    for cookie_value in (None, "wrong-binding"):
        request = _request(
            "/auth/callback",
            cookies=({FLOW_COOKIE: cookie_value} if cookie_value is not None else None),
        )
        with pytest.raises(HTTPException) as denied:
            oidc_callback(
                request,
                state=state,
                code="code",
                session=isolated_session,
            )
        assert denied.value.status_code == 403
        assert denied.value.detail == "oidc_flow_cookie_mismatch"
        isolated_session.refresh(transaction)
        assert transaction.used_at is None


def test_expired_state_is_rejected_without_consumption(
    isolated_session,
) -> None:
    state = "expired-state"
    transaction = _transaction(
        isolated_session,
        identifier="expired",
        state=state,
        expired=True,
    )
    request = _request(
        "/auth/callback",
        cookies={FLOW_COOKIE: flow_binding(state)},
    )
    with pytest.raises(HTTPException) as denied:
        oidc_callback(
            request,
            state=state,
            code="code",
            session=isolated_session,
        )
    assert denied.value.status_code == 410
    assert denied.value.detail == "oidc_state_expired"
    isolated_session.refresh(transaction)
    assert transaction.used_at is None


def test_pkce_or_nonce_failure_leaves_state_unused(
    isolated_session,
    monkeypatch,
) -> None:
    state = "token-validation-state"
    transaction = _transaction(
        isolated_session,
        identifier="token-validation",
        state=state,
    )
    monkeypatch.setattr(
        auth_bff,
        "_exchange_code",
        lambda code, verifier, request: {"id_token": "invalid"},
    )

    def reject_token(raw, expected_nonce=None):
        raise HTTPException(401, "invalid_oidc_nonce")

    monkeypatch.setattr(auth_bff, "_validate_id_token", reject_token)
    request = _request(
        "/auth/callback",
        cookies={FLOW_COOKIE: flow_binding(state)},
    )
    with pytest.raises(HTTPException) as denied:
        oidc_callback(
            request,
            state=state,
            code="code",
            session=isolated_session,
        )
    assert denied.value.status_code == 401
    isolated_session.refresh(transaction)
    assert transaction.used_at is None


def test_success_consumes_state_clears_only_matching_flow_and_replay_fails(
    isolated_session,
    monkeypatch,
) -> None:
    state = "successful-state"
    transaction = _transaction(
        isolated_session,
        identifier="successful",
        state=state,
    )
    _successful_callback_dependencies(monkeypatch)
    request = _request(
        "/auth/callback",
        cookies={FLOW_COOKIE: flow_binding(state)},
    )

    response = oidc_callback(
        request,
        state=state,
        code="code",
        session=isolated_session,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    cookies = _cookies(response)
    assert "__Host-klyrow_session=opaque-browser-session" in cookies
    assert f"{FLOW_COOKIE}=" in cookies
    assert "Max-Age=0" in cookies
    flow_cookie = _cookie_line(response, FLOW_COOKIE)
    assert "Path=/" in flow_cookie
    assert "Path=/auth" not in flow_cookie
    assert "Domain=" not in flow_cookie
    isolated_session.refresh(transaction)
    assert transaction.used_at is not None

    with pytest.raises(HTTPException) as replay:
        oidc_callback(
            request,
            state=state,
            code="code",
            session=isolated_session,
        )
    assert replay.value.status_code == 410
    assert replay.value.detail == "oidc_state_invalid_or_used"


def test_two_concurrent_tab_flows_can_complete_in_either_order(
    isolated_session,
    monkeypatch,
) -> None:
    first_state = "first-tab-state"
    second_state = "second-tab-state"
    first = _transaction(
        isolated_session,
        identifier="first-tab",
        state=first_state,
    )
    second = _transaction(
        isolated_session,
        identifier="second-tab",
        state=second_state,
    )
    _successful_callback_dependencies(monkeypatch)

    both = encode_flow_cookie([flow_binding(first_state), flow_binding(second_state)])
    first_response = oidc_callback(
        _request(
            "/auth/callback",
            cookies={FLOW_COOKIE: both},
        ),
        state=first_state,
        code="code",
        session=isolated_session,
    )
    remaining = _cookie_value(first_response, FLOW_COOKIE)
    assert parse_flow_cookie(remaining) == [flow_binding(second_state)]
    isolated_session.refresh(first)
    isolated_session.refresh(second)
    assert first.used_at is not None
    assert second.used_at is None

    second_response = oidc_callback(
        _request(
            "/auth/callback",
            cookies={FLOW_COOKIE: remaining},
        ),
        state=second_state,
        code="code",
        session=isolated_session,
    )
    assert second_response.status_code == 303
    assert "Max-Age=0" in _cookie_line(second_response, FLOW_COOKIE)
    isolated_session.refresh(second)
    assert second.used_at is not None


def test_latest_single_binding_still_rejects_an_unbound_older_flow(
    isolated_session,
    monkeypatch,
) -> None:
    first_state = "legacy-first-tab-state"
    second_state = "legacy-second-tab-state"
    first = _transaction(
        isolated_session,
        identifier="legacy-first-tab",
        state=first_state,
    )
    second = _transaction(
        isolated_session,
        identifier="legacy-second-tab",
        state=second_state,
    )
    _successful_callback_dependencies(monkeypatch)

    latest_cookie = flow_binding(second_state)
    with pytest.raises(HTTPException) as denied:
        oidc_callback(
            _request(
                "/auth/callback",
                cookies={FLOW_COOKIE: latest_cookie},
            ),
            state=first_state,
            code="code",
            session=isolated_session,
        )
    assert denied.value.status_code == 403
    isolated_session.refresh(first)
    assert first.used_at is None

    response = oidc_callback(
        _request(
            "/auth/callback",
            cookies={FLOW_COOKIE: latest_cookie},
        ),
        state=second_state,
        code="code",
        session=isolated_session,
    )
    assert response.status_code == 303
    isolated_session.refresh(second)
    assert second.used_at is not None


def test_failure_after_state_claim_rolls_back_claim_and_session(
    isolated_session,
    monkeypatch,
) -> None:
    state = "rollback-state"
    transaction = _transaction(
        isolated_session,
        identifier="rollback",
        state=state,
    )
    _token_success(monkeypatch)

    def reject_identity(session, claims):
        raise HTTPException(409, "identity_link_review_required")

    monkeypatch.setattr(
        security,
        "_resolve_identity_context_uncommitted",
        reject_identity,
    )

    with pytest.raises(HTTPException) as denied:
        oidc_callback(
            _request(
                "/auth/callback",
                cookies={FLOW_COOKIE: flow_binding(state)},
            ),
            state=state,
            code="code",
            session=isolated_session,
        )
    assert denied.value.status_code == 409
    isolated_session.refresh(transaction)
    assert transaction.used_at is None
    assert isolated_session.scalar(select(BrowserSession)) is None


def test_terminal_provider_error_consumes_only_its_bound_state(
    isolated_session,
) -> None:
    failed_state = "provider-error-state"
    other_state = "other-open-state"
    failed = _transaction(
        isolated_session,
        identifier="provider-error",
        state=failed_state,
    )
    other = _transaction(
        isolated_session,
        identifier="other-open",
        state=other_state,
    )
    both = encode_flow_cookie([flow_binding(failed_state), flow_binding(other_state)])

    response = oidc_callback(
        _request(
            "/auth/callback",
            cookies={FLOW_COOKIE: both},
        ),
        state=failed_state,
        error="access_denied",
        session=isolated_session,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/service-error"
    assert parse_flow_cookie(_cookie_value(response, FLOW_COOKIE)) == [flow_binding(other_state)]
    isolated_session.refresh(failed)
    isolated_session.refresh(other)
    assert failed.used_at is not None
    assert other.used_at is None


def test_recovery_response_sets_host_only_flow_cookie(
    isolated_session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auth_bff,
        "_authorization_url",
        lambda *args, **kwargs: (
            "https://auth.codestra.co/realms/codestra/"
            "protocol/openid-connect/auth?state=recovery-state"
        ),
    )
    monkeypatch.setattr(
        security,
        "auth_rate",
        lambda *args, **kwargs: None,
    )
    response = begin_recovery(
        RecoveryRequest(email="member@example.com"),
        _request("/auth/actions/recover", method="POST"),
        session=isolated_session,
    )
    cookies = _cookies(response)
    assert response.status_code == 202
    assert f"{FLOW_COOKIE}={flow_binding('recovery-state')}" in cookies
    flow_cookie = _cookie_line(response, FLOW_COOKIE)
    assert "Secure" in flow_cookie
    assert "HttpOnly" in flow_cookie
    assert "SameSite=lax" in flow_cookie
    assert "Path=/" in flow_cookie
    assert "Path=/auth" not in flow_cookie
    assert "Domain=" not in flow_cookie


def test_recovery_adds_binding_without_destroying_another_tab(
    isolated_session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auth_bff,
        "_authorization_url",
        lambda *args, **kwargs: (
            "https://auth.codestra.co/realms/codestra/"
            "protocol/openid-connect/auth?state=recovery-second"
        ),
    )
    monkeypatch.setattr(security, "auth_rate", lambda *args, **kwargs: None)
    response = begin_recovery(
        RecoveryRequest(email="member@example.com"),
        _request(
            "/auth/actions/recover",
            method="POST",
            cookies={FLOW_COOKIE: flow_binding("existing-flow")},
        ),
        session=isolated_session,
    )
    assert parse_flow_cookie(_cookie_value(response, FLOW_COOKIE)) == [
        flow_binding("existing-flow"),
        flow_binding("recovery-second"),
    ]


def _add_invitation(
    session,
    *,
    invitation_id: str,
    role: str,
    active_role: str,
    active: bool = True,
):
    membership = TenantMember(
        id="membership-" + invitation_id,
        tenant_id="tenant-a",
        user_id="user-a",
        role=active_role,
        active=active,
    )
    invitation = TenantInvitation(
        id=invitation_id,
        tenant_id="tenant-a",
        email="member@example.com",
        role=role,
        token_hash=auth_bff.sha("token-" + invitation_id),
        expires_at=auth_bff.now() + timedelta(hours=1),
        created_by="creator",
    )
    session.add_all([membership, invitation])
    session.commit()
    return membership, invitation


def test_selected_invitation_cannot_rewrite_active_member_role(
    isolated_session,
) -> None:
    membership, invitation = _add_invitation(
        isolated_session,
        invitation_id="role-conflict",
        role="READ_ONLY",
        active_role="ADMIN",
    )
    identity = isolated_session.get(OidcIdentity, "identity-a")
    user = isolated_session.get(User, "user-a")

    with pytest.raises(HTTPException) as denied:
        accept_selected_invitation(
            isolated_session,
            identity,
            user,
            {
                "email": "member@example.com",
                "email_verified": True,
            },
            invitation.id,
        )

    assert denied.value.status_code == 409
    assert denied.value.detail == "invitation_existing_member_role_change_denied"
    isolated_session.refresh(membership)
    isolated_session.refresh(invitation)
    assert membership.role == "ADMIN"
    assert membership.active is True
    assert invitation.accepted_at is None


def test_invitation_conflict_rolls_back_oidc_state_claim(
    isolated_session,
    monkeypatch,
) -> None:
    membership, invitation = _add_invitation(
        isolated_session,
        invitation_id="callback-role-conflict",
        role="READ_ONLY",
        active_role="ADMIN",
    )
    state = "invitation-conflict-state"
    transaction = _transaction(
        isolated_session,
        identifier="invitation-conflict",
        state=state,
        mode="invite:" + invitation.id,
    )
    _token_success(
        monkeypatch,
        {
            "iss": ISSUER,
            "sub": "subject-a",
            "aud": "klyrow-portal",
            "email": "member@example.com",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(auth_bff, "_canonical_issuer", lambda: ISSUER)

    with pytest.raises(HTTPException) as denied:
        oidc_callback(
            _request(
                "/auth/callback",
                cookies={FLOW_COOKIE: flow_binding(state)},
            ),
            state=state,
            code="code",
            session=isolated_session,
        )

    assert denied.value.status_code == 409
    isolated_session.refresh(transaction)
    isolated_session.refresh(invitation)
    isolated_session.refresh(membership)
    assert transaction.used_at is None
    assert invitation.accepted_at is None
    assert membership.role == "ADMIN"


def test_same_role_acceptance_preserves_authority(
    isolated_session,
) -> None:
    membership, invitation = _add_invitation(
        isolated_session,
        invitation_id="same-role",
        role="ADMIN",
        active_role="ADMIN",
    )
    identity = isolated_session.get(OidcIdentity, "identity-a")
    user = isolated_session.get(User, "user-a")

    accepted = accept_selected_invitation(
        isolated_session,
        identity,
        user,
        {
            "email": "member@example.com",
            "email_verified": True,
        },
        invitation.id,
    )
    isolated_session.commit()

    assert accepted.id == membership.id
    assert accepted.role == "ADMIN"
    assert accepted.active is True
    assert invitation.accepted_at is not None


def test_inactive_membership_is_reactivated_by_explicit_invitation(
    isolated_session,
) -> None:
    membership, invitation = _add_invitation(
        isolated_session,
        invitation_id="reactivate",
        role="MARKETING",
        active_role="READ_ONLY",
        active=False,
    )
    identity = isolated_session.get(OidcIdentity, "identity-a")
    user = isolated_session.get(User, "user-a")

    accepted = accept_selected_invitation(
        isolated_session,
        identity,
        user,
        {
            "email": "member@example.com",
            "email_verified": True,
        },
        invitation.id,
    )
    isolated_session.commit()

    assert accepted.id == membership.id
    assert accepted.role == "MARKETING"
    assert accepted.active is True
    assert invitation.accepted_at is not None


def test_legacy_invitation_path_also_denies_silent_role_change(
    isolated_session,
) -> None:
    membership, invitation = _add_invitation(
        isolated_session,
        invitation_id="legacy-conflict",
        role="READ_ONLY",
        active_role="ADMIN",
    )
    with pytest.raises(HTTPException) as denied:
        accept_legacy_invitation(
            AcceptIn(token="token-legacy-conflict"),
            session=isolated_session,
        )
    assert denied.value.status_code == 409
    isolated_session.refresh(membership)
    isolated_session.refresh(invitation)
    assert membership.role == "ADMIN"
    assert invitation.accepted_at is None


def test_stage_new_session_never_commits_by_itself(
    isolated_session,
) -> None:
    membership = TenantMember(
        id="stage-membership",
        tenant_id="tenant-a",
        user_id="user-a",
        role="OWNER",
        active=True,
    )
    isolated_session.add(membership)
    isolated_session.commit()
    identity = isolated_session.get(OidcIdentity, "identity-a")
    user = isolated_session.get(User, "user-a")

    item, _raw, _csrf = security._stage_new_session(
        isolated_session,
        _request("/auth/callback"),
        identity,
        user,
        membership,
        {"id_token": "id-token", "refresh_token": "refresh-token"},
    )
    assert item in isolated_session.new
    item_id = item.id
    isolated_session.rollback()
    assert isolated_session.get(BrowserSession, item_id) is None


def test_step_up_rotation_commits_new_and_old_session_atomically(
    isolated_session,
    monkeypatch,
) -> None:
    membership = TenantMember(
        id="step-up-membership",
        tenant_id="tenant-a",
        user_id="user-a",
        role="OWNER",
        active=True,
    )
    old = BrowserSession(
        id="old-session",
        token_hash=auth_bff.sha("old-browser-session"),
        csrf_hash=auth_bff.sha("old-csrf"),
        identity_id="identity-a",
        user_id="user-a",
        tenant_id="tenant-a",
        role="OWNER",
        expires_at=auth_bff.now() + timedelta(minutes=15),
    )
    isolated_session.add_all([membership, old])
    isolated_session.commit()

    state = "step-up-callback-state"
    transaction = _transaction(
        isolated_session,
        identifier="step-up-callback",
        state=state,
        mode="step-up:" + old.id,
    )
    _token_success(
        monkeypatch,
        {
            "iss": ISSUER,
            "sub": "subject-a",
            "aud": "klyrow-portal",
            "email": "member@example.com",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(auth_bff, "_canonical_issuer", lambda: ISSUER)
    monkeypatch.setattr(
        security,
        "_stage_postal_provisioning",
        lambda session, tenant_id: SimpleNamespace(tenant_id=tenant_id),
    )

    response = oidc_callback(
        _request(
            "/auth/callback",
            cookies={FLOW_COOKIE: flow_binding(state)},
        ),
        state=state,
        code="code",
        session=isolated_session,
    )

    isolated_session.refresh(old)
    isolated_session.refresh(transaction)
    new_session = isolated_session.scalar(
        select(BrowserSession).where(BrowserSession.rotated_from_id == old.id)
    )
    assert response.status_code == 303
    assert old.revoked_at is not None
    assert transaction.used_at is not None
    assert new_session is not None
    assert new_session.identity_id == old.identity_id
    assert new_session.tenant_id == old.tenant_id


def test_browser_send_requires_canonical_mail_send_capability(
    monkeypatch,
) -> None:
    payload = MailIn(
        to="recipient@example.com",
        sender="sender@example.com",
        subject="Subject",
        text="Body",
        html="<p>Body</p>",
        stream="transactional",
    )
    calls = []

    async def fake_send(message, ctx, session, idempotency_key):
        calls.append((message, ctx, session, idempotency_key))
        return {"accepted": True}

    monkeypatch.setattr(tenancy_onboarding, "_send", fake_send)

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            browser_send(
                payload,
                ctx={"role": "READ_ONLY", "tenant": "tenant-a"},
                _session=SimpleNamespace(),
                session=SimpleNamespace(),
                idempotency_key="deny-key",
            )
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "mail_send_permission_required"
    assert calls == []

    result = asyncio.run(
        browser_send(
            payload,
            ctx={"role": "MARKETING", "tenant": "tenant-a"},
            _session=SimpleNamespace(),
            session=SimpleNamespace(),
            idempotency_key="allow-key",
        )
    )
    assert result == {"accepted": True}
    assert calls[0][3] == "allow-key"


def test_owner_step_up_is_browser_bound_and_preserves_other_flow(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auth_bff,
        "_authorization_url",
        lambda *args, **kwargs: (
            "https://auth.codestra.co/realms/codestra/"
            "protocol/openid-connect/auth?state=step-up-state"
        ),
    )
    response = platform_owner_step_up(
        _request(
            "/auth/step-up",
            cookies={FLOW_COOKIE: flow_binding("other-state")},
        ),
        return_to="/admin",
        ctx={"sid": "owner-session"},
        session=SimpleNamespace(),
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == ["step-up-state"]
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["0"]
    assert parse_flow_cookie(_cookie_value(response, FLOW_COOKIE)) == [
        flow_binding("other-state"),
        flow_binding("step-up-state"),
    ]
