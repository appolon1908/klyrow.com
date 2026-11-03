"""Mutation authorization must precede tenant lookup, writes, and external calls."""

import inspect

import pytest
from fastapi import HTTPException
from test_middleware_email_contract import gateway as gateway  # pytest fixture re-export

from apps.gateway.app import messaging, production_api, provider, saas

MUTATIONS = (
    [(saas, name) for name in ("profile_upsert", "ingest", "ingest_batch")]
    + [
        (production_api, name)
        for name in (
            "list_create",
            "list_patch",
            "list_delete",
            "template_patch",
            "template_delete",
        )
    ]
    + [
        (messaging, name)
        for name in (
            "domain_claim",
            "domain_verify",
            "dkim_rotate",
            "sender_create",
            "template_create",
            "template_update",
            "template_rollback",
            "template_publish",
            "campaign_create",
            "campaign_test",
            "campaign_schedule",
            "campaign_cancel",
        )
    ]
    + [
        (provider, name)
        for name in (
            "domain_register",
            "domain_verify",
            "domain_dns_check",
            "domain_suspend",
            "dkim_verify",
            "sender_create",
            "sender_suspend",
            "smtp_credential_rotate",
            "smtp_credential_revoke",
        )
    ]
)


@pytest.mark.parametrize("role", ["READ_ONLY", "ANALYST", "BILLING", "service"])
@pytest.mark.parametrize(
    "module,name", MUTATIONS, ids=[m.__name__.split(".")[-1] + "." + n for m, n in MUTATIONS]
)
def test_missing_capability_is_denied_before_access(module, name, role):
    function = getattr(module, name)
    # Invalid state inputs deliberately fail if the permission check is late.
    arguments = {
        name: None
        for name, parameter in inspect.signature(function).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    arguments.update(
        ctx={"role": role, "permissions": ["mail.read"], "tenant": "tenant-a", "sub": "reader"},
        s=None,
    )
    with pytest.raises(HTTPException) as denied:
        function(**arguments)
    assert denied.value.status_code == 403


def test_list_http_crud_requires_explicit_grant_and_keeps_tenant_isolation(gateway):
    client, _, context = gateway
    context.update(role="service", permissions=["mail.read"])
    assert client.post("/v1/lists", json={"name": "Contacts"}).status_code == 403
    context["permissions"] = ["contact.manage"]
    created = client.post("/v1/lists", json={"name": "Contacts"})
    assert created.status_code == 201, created.text
    path = "/v1/lists/" + created.json()["id"]
    context["tenant"] = "tenant-b"
    assert client.patch(path, json={"name": "Hijacked"}).status_code == 404
    assert client.delete(path).status_code == 404
    context["tenant"] = "tenant-a"
    context["permissions"] = ["mail.read"]
    assert client.patch(path, json={"name": "Hijacked"}).status_code == 403
    assert client.delete(path).status_code == 403
    assert client.get(path).json()["name"] == "Contacts"
    context["permissions"] = ["contact.manage"]
    assert client.patch(path, json={"name": "Updated"}).status_code == 200
    assert client.delete(path).status_code == 204


@pytest.mark.parametrize("path", ["/v1/domains/claims", "/v1/internal/email/domains/register"])
def test_domain_http_creation_requires_domain_capability(gateway, path):
    client, _, context = gateway
    context.update(role="service", permissions=["mail.send"])
    assert client.post(path, json={"domain": "scope.example"}).status_code == 403
    context["permissions"] = ["domain.manage"]
    response = client.post(path, json={"domain": "scope.example"})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "method,path,payload,permission,expected",
    [
        ("POST", "/v1/lists", {"name": "Resolver contacts"}, "contact.manage", 201),
        ("POST", "/v1/profiles", {"external_id": "resolver-profile"}, "contact.manage", 201),
        (
            "POST",
            "/v1/events",
            {"profile_id": "absent", "name": "synthetic"},
            "contact.manage",
            404,
        ),
        (
            "POST",
            "/v1/events/batch",
            {"events": [{"profile_id": "absent", "name": "synthetic"}]},
            "contact.manage",
            207,
        ),
        ("POST", "/v1/domains/claims", {"domain": "resolver.example"}, "domain.manage", 201),
        (
            "POST",
            "/v1/internal/email/domains/register",
            {"domain": "resolver.example"},
            "domain.manage",
            201,
        ),
        ("DELETE", "/v1/templates/foreign", None, "template.manage", 404),
        ("POST", "/v1/campaign-definitions/foreign/cancel", None, "campaign.manage", 404),
        ("POST", "/v1/internal/email/senders/foreign/suspend", None, "sender.manage", 404),
        (
            "POST",
            "/v1/internal/email/smtp/credentials/foreign/revoke",
            None,
            "credential.manage",
            404,
        ),
    ],
)
def test_real_resolver_requests_exact_mutation_authority(
    gateway, monkeypatch, method, path, payload, permission, expected
):
    import httpx

    from apps.gateway.app import main as core

    client, _, _ = gateway
    core.app.dependency_overrides.pop(core.auth)
    monkeypatch.setenv("KLYROW_TENANT_RESOLVER_URL", "https://resolver.invalid/resolve")
    requested = []

    def resolve(url, *, headers, **kwargs):
        requested.append(headers["X-Codestra-Required-Permission"])
        assert headers["X-Klyrow-Tenant-Id"] == "tenant-a"
        return httpx.Response(
            200,
            json={
                "authorized": True,
                "permission": permission,
                "identity_id": "scoped-service",
                "tenant_id": "tenant-a",
                "role": "service",
            },
        )

    monkeypatch.setattr(core.httpx, "get", resolve)
    headers = {"Authorization": "Bearer synthetic-scoped-token", "X-Tenant-ID": "tenant-a"}
    response = client.request(method, path, json=payload, headers=headers)
    assert response.status_code == expected, response.text
    assert requested == [permission]
    monkeypatch.setattr(
        core.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200,
            json={
                "authorized": True,
                "permission": "klyrow.send",
                "identity_id": "scoped-service",
                "tenant_id": "tenant-a",
                "role": "service",
            },
        ),
    )
    assert client.request(method, path, json=payload, headers=headers).status_code == 403
