"""M1-01 (security finding H2): only OWNER may assign or revoke the OWNER role.

Covers the browser team API (``/app/api/team/*``), the production invitation
flow override, the legacy bearer API (``/v1/team/*``) and the pure guard.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import DB, Base, User, engine, ph, sha
from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff
from apps.gateway.app.auth_bff import BrowserSession
from apps.gateway.app.tenancy import OidcIdentity, TenantMember, assert_role_assignable
from apps.gateway.app.tenancy_onboarding import resolve_identity_context

issuer = "https://auth.codestra.co/realms/codestra"
client = TestClient(app, base_url="https://app.klyrow.test")

OWNER_SUBJECT = "guard-owner"
TENANT: dict[str, str] = {}
IDENTITY: dict[str, str] = {}


def setup_module():
    os.environ["KLYROW_OIDC_ISSUER"] = issuer
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        identity, user, membership = resolve_identity_context(
            s,
            {
                "sub": OWNER_SUBJECT,
                "email": "guard-owner@example.com",
                "email_verified": True,
                "name": "Guard Workspace",
            },
        )
        assert membership.role == "OWNER"
        TENANT["id"] = membership.tenant_id
        TENANT["owner_user"] = user.id
        IDENTITY[user.id] = identity.id
        for key, role in (
            ("admin_user", "ADMIN"),
            ("second_owner_user", "OWNER"),
            ("developer_user", "DEVELOPER"),
            ("support_user", "SUPPORT"),
        ):
            uid = f"guard-{key}"
            s.add(
                User(
                    id=uid,
                    tenant_id=membership.tenant_id,
                    email=f"{uid}@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role="tenant_admin",
                )
            )
            s.add(
                TenantMember(
                    id=str(uuid.uuid4()),
                    tenant_id=membership.tenant_id,
                    user_id=uid,
                    role=role,
                    active=True,
                )
            )
            identity_id = str(uuid.uuid4())
            s.add(
                OidcIdentity(
                    id=identity_id,
                    issuer=issuer,
                    subject=f"subject-{uid}",
                    user_id=uid,
                    default_tenant_id=membership.tenant_id,
                )
            )
            IDENTITY[uid] = identity_id
            TENANT[key] = uid
        s.commit()


def _browser_session(user_id: str) -> tuple[str, str]:
    """Create a browser session for ``user_id`` and return (cookie, csrf)."""

    raw = f"session-{user_id}-{uuid.uuid4()}"
    csrf = f"csrf-{uuid.uuid4()}"
    with DB() as s:
        member = s.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == TENANT["id"], TenantMember.user_id == user_id
            )
        )
        s.add(
            BrowserSession(
                id=str(uuid.uuid4()),
                token_hash=sha(raw),
                csrf_hash=sha(csrf),
                identity_id=IDENTITY[user_id],
                user_id=user_id,
                tenant_id=TENANT["id"],
                role=member.role,
                created_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )
        s.commit()
    return raw, csrf


def _as(user_key: str):
    raw, csrf = _browser_session(TENANT[user_key])
    client.cookies.set(auth_bff.SESSION_COOKIE, raw, path="/")
    return {"X-Klyrow-CSRF": csrf}


def _member_role(user_id: str) -> str:
    with DB() as s:
        return s.scalar(
            select(TenantMember.role).where(
                TenantMember.tenant_id == TENANT["id"], TenantMember.user_id == user_id
            )
        )


def test_guard_matrix():
    assert_role_assignable("OWNER", "OWNER")
    assert_role_assignable("OWNER", "ADMIN", current_role="OWNER")
    assert_role_assignable("ADMIN", "DEVELOPER")
    assert_role_assignable("ADMIN", "READ_ONLY", current_role="DEVELOPER")
    assert_role_assignable("platform_admin", "OWNER")
    for actor, target, current in (
        ("ADMIN", "OWNER", None),
        ("ADMIN", "ADMIN", None),
        ("ADMIN", "DEVELOPER", "OWNER"),
        ("DEVELOPER", "DEVELOPER", None),
        ("DEVELOPER", "OWNER", None),
    ):
        with pytest.raises(HTTPException) as exc:
            assert_role_assignable(actor, target, current_role=current)
        assert exc.value.status_code == 403
        assert exc.value.detail == "role_escalation_forbidden"


def test_admin_cannot_promote_member_to_owner():
    headers = _as("admin_user")
    response = client.patch(
        f"/app/api/team/{TENANT['developer_user']}", headers=headers, json={"role": "OWNER"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "role_escalation_forbidden"
    assert _member_role(TENANT["developer_user"]) == "DEVELOPER"


def test_admin_cannot_promote_self_to_owner():
    headers = _as("admin_user")
    response = client.patch(
        f"/app/api/team/{TENANT['admin_user']}", headers=headers, json={"role": "owner"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "role_escalation_forbidden"
    assert _member_role(TENANT["admin_user"]) == "ADMIN"


def test_admin_cannot_invite_owner():
    headers = _as("admin_user")
    response = client.post(
        "/app/api/team/invitations",
        headers=headers,
        json={"email": "new-owner@example.com", "role": "OWNER"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "role_escalation_forbidden"


def test_admin_cannot_assign_admin():
    headers = _as("admin_user")
    response = client.patch(
        f"/app/api/team/{TENANT['developer_user']}", headers=headers, json={"role": "ADMIN"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "role_escalation_forbidden"
    invited = client.post(
        "/app/api/team/invitations",
        headers=headers,
        json={"email": "new-admin@example.com", "role": "ADMIN"},
    )
    assert invited.status_code == 403, invited.text
    assert invited.json()["detail"] == "role_escalation_forbidden"
    assert _member_role(TENANT["developer_user"]) == "DEVELOPER"


def test_admin_can_assign_developer():
    headers = _as("admin_user")
    response = client.patch(
        f"/app/api/team/{TENANT['support_user']}", headers=headers, json={"role": "DEVELOPER"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "DEVELOPER"
    invited = client.post(
        "/app/api/team/invitations",
        headers=headers,
        json={"email": "new-dev@example.com", "role": "DEVELOPER"},
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["role"] == "DEVELOPER"


def test_admin_cannot_change_an_owners_role():
    headers = _as("admin_user")
    # Two active owners exist, so the last-owner protection does not apply here:
    # the refusal must come from the escalation guard.
    response = client.patch(
        f"/app/api/team/{TENANT['second_owner_user']}", headers=headers, json={"role": "DEVELOPER"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "role_escalation_forbidden"
    assert _member_role(TENANT["second_owner_user"]) == "OWNER"


def test_admin_cannot_remove_an_owner():
    headers = _as("admin_user")
    # Two active owners exist, so last-owner protection does not apply; the
    # refusal must come from the escalation guard (revoking OWNER is owner-only).
    response = client.delete(f"/app/api/team/{TENANT['second_owner_user']}", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "role_escalation_forbidden"
    assert _member_role(TENANT["second_owner_user"]) == "OWNER"


def test_role_rank_covers_every_permission_role():
    from apps.gateway.app.tenancy import ROLE_PERMISSIONS, ROLE_RANK

    assert set(ROLE_RANK) == set(ROLE_PERMISSIONS)


def test_owner_can_assign_owner_and_last_owner_protection_remains():
    headers = _as("owner_user")
    response = client.patch(
        f"/app/api/team/{TENANT['developer_user']}", headers=headers, json={"role": "OWNER"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "OWNER"
    invited = client.post(
        "/app/api/team/invitations",
        headers=headers,
        json={"email": "invited-owner@example.com", "role": "OWNER"},
    )
    assert invited.status_code == 201, invited.text
    # An owner may demote another owner while more than one owner remains...
    demoted = client.patch(
        f"/app/api/team/{TENANT['developer_user']}", headers=headers, json={"role": "DEVELOPER"}
    )
    assert demoted.status_code == 200, demoted.text
    demoted = client.patch(
        f"/app/api/team/{TENANT['second_owner_user']}", headers=headers, json={"role": "ADMIN"}
    )
    assert demoted.status_code == 200, demoted.text
    # ...but the last owner stays protected.
    last = client.patch(
        f"/app/api/team/{TENANT['owner_user']}", headers=headers, json={"role": "ADMIN"}
    )
    assert last.status_code == 409, last.text
    assert last.json()["detail"] == "last_owner_protected"


def _legacy_login(user_id: str) -> dict:
    response = client.post(
        "/v1/auth/login",
        json={"email": f"{user_id}@example.com", "password": "long-enough-password"},
    )
    assert response.status_code == 200, response.text
    switched = client.post(
        f"/v1/organizations/{TENANT['id']}/switch",
        headers={"Authorization": "Bearer " + response.json()["access_token"]},
    )
    assert switched.status_code == 200, switched.text
    return {"Authorization": "Bearer " + switched.json()["access_token"]}


def test_legacy_api_admin_cannot_grant_owner():
    client.cookies.clear()
    admin = _legacy_login(TENANT["admin_user"])
    invited = client.post(
        "/v1/team/invitations",
        headers=admin,
        json={"email": "legacy-owner@example.com", "role": "OWNER"},
    )
    assert invited.status_code == 403, invited.text
    assert invited.json()["detail"] == "role_escalation_forbidden"
    promoted = client.patch(
        f"/v1/team/members/{TENANT['developer_user']}", headers=admin, json={"role": "OWNER"}
    )
    assert promoted.status_code == 403, promoted.text
    assert promoted.json()["detail"] == "role_escalation_forbidden"
    allowed = client.post(
        "/v1/team/invitations",
        headers=admin,
        json={"email": "legacy-dev@example.com", "role": "DEVELOPER"},
    )
    assert allowed.status_code == 201, allowed.text
