"""Owner APIs exercise the real authenticator, RSA signature and DB authority."""

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app import main
from apps.gateway.app.platform import app
from apps.gateway.app.platform_owner_policy import CANONICAL_ISSUER
from apps.gateway.app.tenancy import OidcIdentity, TenantMember

client = TestClient(app)
STATUS = "/v1/admin/security/platform-owner"


@pytest.fixture(autouse=True)
def owner_database(monkeypatch):
    monkeypatch.setenv("KLYROW_EMBEDDED_WORKERS", "false")
    monkeypatch.setenv("KLYROW_SAFE_MODE", "true")
    monkeypatch.setenv("KLYROW_RATE_PER_MINUTE", "1000")
    monkeypatch.setenv("LIVE_EMAIL_DELIVERY", "false")
    main.rate_buckets.clear()
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "DB", sessionmaker(bind=engine, expire_on_commit=False))
    main.Base.metadata.create_all(engine)
    with main.DB() as session:
        session.add(main.Tenant(id="root", name="Owner fixture", quota=0))
        session.add(main.User(
            id="root", tenant_id="root", email="root@example.com",
            password_hash="unused-oidc-fixture", role="platform_admin", enabled=True,
        ))
        session.commit()


def test_owner_authority_readback_is_authenticated_and_redacted(canonical_api_owner):
    assert client.get(STATUS).status_code == 401
    response = client.get(STATUS, headers=canonical_api_owner())
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-Id"]
    assert response.json() == {
        "authority": "PLATFORM_OWNER", "identity_binding": "issuer_subject",
        "mailbox_verified": True, "mfa_verified": True,
        "fresh_authentication_verified": True, "authentication_max_age_seconds": 300,
    }
    assert "root@example.com" not in response.text
    assert "8f52e865" not in response.text


@pytest.mark.parametrize("updates,detail", [
    ({"email_verified": False}, "platform_owner_email_unverified"),
    ({"email": "different@example.com"}, "platform_owner_email_unverified"),
    ({"amr": ["pwd"]}, "platform_owner_mfa_required"),
    ({"auth_time": 1}, "platform_owner_step_up_required"),
])
def test_owner_api_enforces_verified_mailbox_mfa_and_freshness(canonical_api_owner, updates, detail):
    response = client.get(STATUS, headers=canonical_api_owner(**updates))
    assert response.status_code == 403
    assert response.json()["detail"] == detail
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("field,value,detail", [
    ("KLYROW_PLATFORM_OWNER_SUBJECT", "", "platform_owner_not_configured"),
    ("KLYROW_PLATFORM_OWNER_EMAIL", "root@gmail", "platform_owner_email_misconfigured"),
    ("KLYROW_PLATFORM_OWNER_ISSUER", CANONICAL_ISSUER + "/", "platform_owner_issuer_misconfigured"),
    ("KLYROW_PLATFORM_OWNER_SUBJECT", "another-subject", "platform_owner_identity_mismatch"),
])
def test_runtime_binding_cannot_be_completed_by_email_alone(canonical_api_owner, monkeypatch, field, value, detail):
    headers = canonical_api_owner()
    monkeypatch.setenv(field, value)
    response = client.get(STATUS, headers=headers)
    assert response.status_code in {403, 503}
    assert response.json()["detail"] == detail


@pytest.mark.parametrize("resource,attribute,value", [
    ("user", "enabled", False),
    ("user", "role", "tenant_admin"),
    ("identity", "enabled", False),
    ("identity", "identity_type", "SERVICE"),
    ("member", "active", False),
    ("member", "role", "tenant_admin"),
])
def test_owner_api_rechecks_current_authority(canonical_api_owner, resource, attribute, value):
    headers = canonical_api_owner()
    with main.DB() as session:
        objects = {
            "user": session.get(main.User, "root"),
            "identity": session.get(OidcIdentity, "owner-api-fixture-identity"),
            "member": session.get(TenantMember, "owner-api-fixture-member"),
        }
        setattr(objects[resource], attribute, value)
        session.commit()
    response = client.get(STATUS, headers=headers)
    assert response.status_code == 403, response.text


def test_local_legacy_owner_token_cannot_grant_platform_authority(monkeypatch):
    monkeypatch.setenv("KLYROW_ENV", "development")
    monkeypatch.setenv("KLYROW_LOCAL_AUTH_ENABLED", "true")
    token = jwt.encode({
        "sub": "root", "tenant": "root", "role": "platform_admin",
        "exp": int(time.time()) + 300,
    }, main.SECRET, algorithm="HS256")
    response = client.get(STATUS, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403
    assert response.json()["detail"] == "platform_owner_oidc_required"


def test_resolver_role_cannot_replace_verified_owner_identity(monkeypatch):
    import httpx
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "https://resolver.test/resolve")
    monkeypatch.setattr(main.httpx, "get", lambda *_a, **_kw: httpx.Response(
        200, json={"authorized": True, "permission": "klyrow.read",
                   "identity_id": "root", "tenant_id": "root", "role": "platform_admin"},
    ))
    response = client.get(STATUS, headers={"Authorization": "Bearer test-service-fixture"})
    assert response.status_code == 403
    assert response.json()["detail"] == "platform_owner_oidc_required"


def test_tenant_role_is_never_promoted_by_exact_identity(canonical_api_owner):
    headers = canonical_api_owner()
    with main.DB() as session:
        session.get(main.User, "root").role = "tenant_admin"
        session.get(TenantMember, "owner-api-fixture-member").role = "tenant_admin"
        session.commit()
    assert client.get(STATUS, headers=headers).status_code == 403
    with main.DB() as session:
        assert session.get(main.User, "root").role == "tenant_admin"


def test_expired_or_wrongly_signed_token_is_rejected(canonical_api_owner):
    expired = canonical_api_owner(exp=int(time.time()) - 1)
    assert client.get(STATUS, headers=expired).status_code == 401
    from cryptography.hazmat.primitives.asymmetric import rsa
    headers = canonical_api_owner()
    claims = jwt.decode(headers["Authorization"][7:], options={"verify_signature": False})
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = jwt.encode(claims, other_key, algorithm="RS256")
    assert client.get(STATUS, headers={"Authorization": "Bearer " + bad}).status_code == 401


def test_every_admin_api_depends_on_core_authenticator():
    from fastapi.routing import APIRoute
    from apps.gateway.app.openapi_authority import _api_route_contexts

    from apps.gateway.app import tenancy

    trusted_authenticators = {main.auth, tenancy.auth}

    def depends_on_auth(dependency):
        return dependency.call in trusted_authenticators or any(
            depends_on_auth(child) for child in dependency.dependencies
        )

    routes = [r for r in _api_route_contexts(app) if r.path.startswith("/v1/admin/")]
    assert len(routes) >= 10
    for route in routes:
        assert depends_on_auth(route.dependant), route.path
    for route in [r for r in _api_route_contexts(app) if r.path.startswith("/app/api/admin/")]:
        from apps.gateway.app.platform_owner import platform_owner_role_stability_guard
        assert any(d.call is platform_owner_role_stability_guard for d in route.dependant.dependencies)


def test_owner_status_openapi_has_auth_and_documented_failure_responses():
    schema = app.openapi()
    for path in (STATUS, "/app/api/admin/security/platform-owner"):
        operation = schema["paths"][path]["get"]
        assert operation["security"]
        assert {"401", "403", "503"} <= operation["responses"].keys()


def test_stale_owner_authentication_blocks_every_admin_write(canonical_api_owner):
    import re
    from fastapi.routing import APIRoute
    from apps.gateway.app.openapi_authority import _api_route_contexts

    headers = canonical_api_owner(auth_time=1)
    writes = [
        (method, re.sub(r"{[^}]+}", "owner-negative-fixture", route.path))
        for route in _api_route_contexts(app)
        if route.path.startswith("/v1/admin/")
        for method in route.methods if method in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert len(writes) >= 8
    for method, path in writes:
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["detail"] == "platform_owner_step_up_required"
    with main.DB() as session:
        assert session.scalar(select(main.Audit)) is None
        assert session.get(main.Tenant, "root").enabled is True


def test_browser_status_refuses_an_unvalidated_authority_marker():
    from fastapi import HTTPException, Response
    from starlette.requests import Request
    from apps.gateway.app.platform_owner_api import owner_authority_status

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}})
    with pytest.raises(HTTPException) as denied:
        owner_authority_status(request, Response(), browser=True)
    assert denied.value.status_code == 403


def test_api_guard_holds_authority_rows_in_browser_compatible_order(canonical_api_owner):
    from types import SimpleNamespace
    from apps.gateway.app.platform_owner_api import validate_api_owner

    headers = canonical_api_owner()
    claims = jwt.decode(headers["Authorization"][7:], options={"verify_signature": False})
    statements = []

    class RecordingSession:
        def scalar(self, statement):
            statements.append(statement)
            if len(statements) == 1:
                return SimpleNamespace(id="root", enabled=True, role="platform_admin")
            if len(statements) == 2:
                return SimpleNamespace(active=True, role="platform_admin")
            return SimpleNamespace(
                enabled=True, user_id="root", subject=claims["sub"],
                issuer=claims["iss"], identity_type="HUMAN",
            )

    validate_api_owner(
        RecordingSession(),
        {"sub": "root", "tenant": "root", "role": "platform_admin", "oidc_sub": claims["sub"]},
        claims=claims, identity_id="owner-api-fixture-identity",
    )
    assert [s.column_descriptions[0]["entity"] for s in statements] == [
        main.User, TenantMember, OidcIdentity,
    ]
    assert all(s._for_update_arg.read is True for s in statements)
    assert all(s.get_execution_options()["populate_existing"] for s in statements)


@pytest.mark.parametrize("resolver_identity_type,expected", [("HUMAN", 200), ("SERVICE", 403)])
def test_resolver_permission_and_local_signed_identity_are_both_required(
    canonical_api_owner, monkeypatch, resolver_identity_type, expected,
):
    import httpx

    headers = canonical_api_owner()
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "https://resolver.test/resolve")
    monkeypatch.setattr(main.httpx, "get", lambda *_a, **_kw: httpx.Response(
        200, json={
            "authorized": True, "permission": "klyrow.read",
            "identity_id": "resolver-directory-identity", "tenant_id": "root",
            "identity_type": resolver_identity_type, "role": "platform_admin",
        },
    ))
    response = client.get(STATUS, headers=headers)
    assert response.status_code == expected, response.text


def test_resolver_cannot_substitute_a_different_tenant(canonical_api_owner, monkeypatch):
    import httpx

    headers = {**canonical_api_owner(), "X-Tenant-ID": "root"}
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "https://resolver.test/resolve")
    monkeypatch.setattr(main.httpx, "get", lambda *_a, **_kw: httpx.Response(
        200, json={
            "authorized": True, "permission": "klyrow.read",
            "identity_id": "directory-identity", "tenant_id": "another-tenant",
            "identity_type": "HUMAN", "role": "platform_admin",
        },
    ))
    assert client.get(STATUS, headers=headers).status_code == 403


def test_postgres_owner_role_demotion_waits_for_request_transaction(monkeypatch):
    import os
    import uuid

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text, update
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session
    from apps.gateway.app.platform_owner_api import validate_api_owner

    url = os.getenv("KLYROW_CONTRACT_POSTGRES_URL")
    if not url:
        pytest.skip("isolated PostgreSQL contract database required")
    admin_engine = create_engine(url)
    schema = "owner_guard_" + uuid.uuid4().hex
    with admin_engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    engine = admin_engine.execution_options(schema_translate_map={None: schema})
    tables = [main.Tenant.__table__, main.User.__table__, OidcIdentity.__table__, TenantMember.__table__]
    try:
        main.Base.metadata.create_all(engine, tables=tables)
        with Session(engine) as session:
            session.add_all([
                main.Tenant(id="owner", name="Owner fixture", quota=0),
                main.User(id="owner", tenant_id="owner", email="owner@example.com",
                          password_hash="unused-oidc-fixture", role="platform_admin", enabled=True),
                OidcIdentity(id="identity", issuer=CANONICAL_ISSUER, subject="owner-subject",
                             user_id="owner", default_tenant_id="owner", identity_type="HUMAN", enabled=True),
                TenantMember(id="member", tenant_id="owner", user_id="owner", role="platform_admin", active=True),
            ])
            session.commit()
        monkeypatch.setenv("KLYROW_PLATFORM_OWNER_ISSUER", CANONICAL_ISSUER)
        monkeypatch.setenv("KLYROW_PLATFORM_OWNER_SUBJECT", "owner-subject")
        monkeypatch.setenv("KLYROW_PLATFORM_OWNER_EMAIL", "owner@example.com")
        now = int(time.time())
        claims = {
            "iss": CANONICAL_ISSUER, "sub": "owner-subject",
            "email": "owner@example.com", "email_verified": True, "amr": ["pwd", "otp"],
            "iat": now, "auth_time": now, "exp": now + 300,
        }
        context = {"sub": "owner", "tenant": "owner", "role": "platform_admin", "oidc_sub": "owner-subject"}
        with Session(engine) as request_session:
            validate_api_owner(request_session, context, claims=claims, identity_id="identity")
            with pytest.raises(DBAPIError) as blocked:
                with engine.begin() as writer:
                    writer.execute(text("SET LOCAL lock_timeout = '150ms'"))
                    writer.execute(update(main.User).where(main.User.id == "owner").values(role="tenant_admin"))
            assert blocked.value.orig.sqlstate == "55P03"
            request_session.rollback()
        with engine.begin() as writer:
            writer.execute(update(main.User).where(main.User.id == "owner").values(role="tenant_admin"))
        with Session(engine) as next_request, pytest.raises(HTTPException) as denied:
            validate_api_owner(next_request, context, claims=claims, identity_id="identity")
        assert denied.value.detail == "platform_admin_required"
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin_engine.dispose()
