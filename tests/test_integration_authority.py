"""Resolver and integration-service authority: synthetic inputs, no network."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.gateway.app import main as core
from apps.gateway.app.operations import trusted_result_auth

RESULT_PERMISSION = "klyrow.integration.result.write"
COMMAND_PERMISSION = "klyrow.middleware.command.write"
OBSERVABILITY_READ_PERMISSION = "klyrow.observability.read"
OBSERVABILITY_WRITE_PERMISSION = "klyrow.observability.write"


class Tenants:
    def __init__(self):
        self.reads = []

    def get(self, model, identity):
        self.reads.append(identity)
        return SimpleNamespace(id=identity, enabled=True)


def resolve(monkeypatch, response, *, requested_tenant="tenant-a"):
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "https://resolver.invalid/resolve")
    monkeypatch.setattr(core.httpx, "get", lambda *a, **kw: httpx.Response(200, json=response))
    core.rate_buckets.clear()
    request = Request({"type": "http", "method": "GET", "path": "/v1/messages",
                       "scheme": "https", "server": ("klyrow.test", 443), "headers": []})
    tenants = Tenants()
    return core.auth(request, authorization="Bearer synthetic-resolver-token",
                     x_klyrow_tenant_id=requested_tenant, x_tenant_id=None, s=tenants), tenants


def grant(**changes):
    return {"authorized": True, "permission": "klyrow.read", "tenant_id": "tenant-a",
            "identity_id": "service-a", "identity_type": "SERVICE", **changes}


def test_resolver_keeps_exact_requested_tenant(monkeypatch):
    with pytest.raises(HTTPException) as denied:
        resolve(monkeypatch, grant(tenant_id="tenant-b"))
    assert (denied.value.status_code, denied.value.detail) == (403, "not_found")


@pytest.mark.parametrize("authorized", ["false", "true", 1, [True], {"value": True}])
def test_resolver_requires_boolean_true_not_truthiness(monkeypatch, authorized):
    with pytest.raises(HTTPException) as denied:
        resolve(monkeypatch, grant(authorized=authorized))
    assert denied.value.status_code == 403


@pytest.mark.parametrize("response", [None, [], ["authorized"], "authorized", 7])
def test_resolver_rejects_non_object_authority(monkeypatch, response):
    with pytest.raises(HTTPException) as denied:
        resolve(monkeypatch, response)
    assert (denied.value.status_code, denied.value.detail) == (503, "authorization_unavailable")


@pytest.mark.parametrize("field", ["identity_id", "tenant_id"])
@pytest.mark.parametrize("value", [None, "", " ", 123, ["identity"], {"id": "identity"}])
def test_resolver_requires_string_identity_fields(monkeypatch, field, value):
    with pytest.raises(HTTPException) as denied:
        resolve(monkeypatch, grant(**{field: value}))
    assert (denied.value.status_code, denied.value.detail) == (503, "authorization_unavailable")


def test_valid_resolver_authority_and_default_tenant_still_work(monkeypatch):
    for requested in (None, "tenant-a"):
        context, tenants = resolve(monkeypatch, grant(), requested_tenant=requested)
        assert context["tenant"] == "tenant-a" and context["sub"] == "service-a"
        assert tenants.reads == ["tenant-a"]


@pytest.mark.parametrize("boundary,permission", [(trusted_result_auth, RESULT_PERMISSION),
                                                   (core.require_middleware_command_scope, COMMAND_PERMISSION)])
@pytest.mark.parametrize("context", [
    {"sub": "middleware-service"},
    {"sub": "middleware-service", "service": True},
    {"sub": "middleware-service", "service": True, "identity_type": "HUMAN"},
    {"sub": "ordinary-human", "service": False, "identity_type": "HUMAN"},
    {"sub": "service-a", "service": "true", "identity_type": "SERVICE"},
])
def test_subject_or_grant_alone_cannot_impersonate_integration_service(boundary, permission, context):
    with pytest.raises(HTTPException) as denied:
        boundary({**context, "permissions": [permission]})
    assert denied.value.status_code == 403


@pytest.mark.parametrize("boundary,permission", [(trusted_result_auth, RESULT_PERMISSION),
                                                   (core.require_middleware_command_scope, COMMAND_PERMISSION)])
@pytest.mark.parametrize("representation", ["list", "string", "set"])
def test_typed_service_with_exact_permission_is_accepted(boundary, permission, representation):
    values = {"list": [permission], "string": "unrelated " + permission, "set": {permission}}
    boundary({"sub": "approved-service", "tenant": "COD", "service": True,
              "identity_type": "SERVICE_ACCOUNT", "scopes": values[representation]})


@pytest.mark.parametrize("boundary", [trusted_result_auth, core.require_middleware_command_scope])
@pytest.mark.parametrize("permissions", [["*"], ["klyrow.send"], {"klyrow.middleware.command.write": True}, [["nested"]], 1])
def test_service_boundaries_fail_closed_on_missing_or_malformed_grants(boundary, permissions):
    with pytest.raises(HTTPException) as denied:
        boundary({"sub": "middleware-service", "service": True, "identity_type": "SERVICE",
                  "permissions": permissions})
    assert denied.value.status_code == 403


def test_authenticated_legacy_middleware_key_has_explicit_service_authority(monkeypatch):
    import secrets
    credential = secrets.token_urlsafe(32)
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "")
    monkeypatch.setattr(core, "runtime_secret", lambda name: credential if name == "KLYROW_MIDDLEWARE_API_KEY" else "")
    core.rate_buckets.clear()
    request = Request({"type": "http", "method": "POST", "path": "/v1/commands",
                       "scheme": "https", "server": ("klyrow.test", 443), "headers": []})
    context = core.auth(request, authorization="Bearer " + credential,
                        x_klyrow_tenant_id="tenant-a", x_tenant_id=None, s=Tenants())
    assert context["service"] is True and context["identity_type"] == "SERVICE"
    assert set(context["permissions"]) == {
        RESULT_PERMISSION,
        COMMAND_PERMISSION,
        OBSERVABILITY_READ_PERMISSION,
        OBSERVABILITY_WRITE_PERMISSION,
    }
    assert trusted_result_auth(context) is context
    core.require_middleware_command_scope(context)
    with pytest.raises(HTTPException) as denied:
        core.auth(request, authorization="Bearer invalid-synthetic-credential",
                  x_klyrow_tenant_id="tenant-a", x_tenant_id=None, s=Tenants())
    assert denied.value.status_code == 401


@pytest.mark.parametrize("identity_type", ["SERVICE", "SERVICE_ACCOUNT", "service", "HUMAN", "KLYROW_ONLY"])
def test_signed_oidc_service_type_comes_from_registered_identity(monkeypatch, identity_type):
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    issuer = "https://auth.codestra.co/realms/codestra"
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "")
    monkeypatch.setenv("KLYROW_OIDC_ISSUER", issuer)
    monkeypatch.setenv("KLYROW_OIDC_AUDIENCE", "klyrow-api")
    monkeypatch.setattr(core, "runtime_secret", lambda name: "")
    monkeypatch.setitem(core._jwks_clients, issuer, SimpleNamespace(
        get_signing_key_from_jwt=lambda raw: SimpleNamespace(key=private.public_key())))
    records = iter([SimpleNamespace(id="registered-identity-a", user_id="service-a", default_tenant_id="tenant-a",
                                    identity_type=identity_type), SimpleNamespace(role="OWNER")])
    tenants = Tenants()
    tenants.scalar = lambda query: next(records)
    claims = {"iss": issuer, "aud": "klyrow-api", "sub": "registered-oidc-subject",
              "iat": int(time.time()), "exp": int(time.time()) + 300,
              "scope": COMMAND_PERMISSION + " " + RESULT_PERMISSION,
              "identity_type": "SERVICE"}  # An untrusted token claim cannot promote a human.
    token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "synthetic"})
    request = Request({"type": "http", "method": "POST", "path": "/v1/commands",
                       "scheme": "https", "server": ("klyrow.test", 443), "headers": []})
    core.rate_buckets.clear()
    context = core.auth(request, authorization="Bearer " + token,
                        x_klyrow_tenant_id="tenant-a", x_tenant_id=None, s=tenants)
    if identity_type.upper() in {"SERVICE", "SERVICE_ACCOUNT"}:
        assert context["service"] is True
        core.require_middleware_command_scope(context)
        assert trusted_result_auth(context) is context
    else:
        assert context["service"] is False
        for boundary in (trusted_result_auth, core.require_middleware_command_scope):
            with pytest.raises(HTTPException) as denied:
                boundary(context)
            assert denied.value.status_code == 403
