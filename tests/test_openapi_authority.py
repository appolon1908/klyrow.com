from apps.gateway.app.openapi_authority import (
    AUDIENCES,
    BROWSER_ANONYMOUS,
    CLASSIFIED_IDEMPOTENCY,
    DURABLE_IDEMPOTENCY,
    NON_ATOMIC_ITEM_IDEMPOTENCY,
    OPTIONAL_IDEMPOTENCY,
    REQUIRED_IDEMPOTENCY,
    runtime_idempotency_routes,
    runtime_route_fingerprint,
    runtime_routes,
)
from apps.gateway.app.platform import app


HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "options",
    "head",
    "trace",
}


def operations(schema):
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                yield method, path, operation


def header_parameter(operation, name):
    return next(
        item
        for item in operation.get("parameters", [])
        if item.get("in") == "header" and item.get("name", "").lower() == name.lower()
    )


def test_every_documented_operation_has_one_canonical_audience_and_auth_model():
    schema = app.openapi()
    rows = list(operations(schema))
    assert len(rows) == schema["x-klyrow-operation-count"]
    assert len(rows) == 368
    assert all(row[2]["x-klyrow-audience"] in AUDIENCES for row in rows)
    assert all(row[2]["x-klyrow-auth-model"] for row in rows)
    assert all("security" in row[2] for row in rows)
    assert all("x-klyrow-enforced-auth-dependencies" in row[2] for row in rows)
    assert sum(schema["x-klyrow-audience-counts"].values()) == len(rows)
    operation_ids = [row[2]["operationId"] for row in rows]
    assert len(operation_ids) == len(set(operation_ids))


def test_complete_runtime_route_table_includes_and_fingerprints_hidden_apis():
    schema = app.openapi()
    rows = runtime_routes(app)
    assert schema["x-klyrow-runtime-operation-count"] == len(rows)
    assert schema["x-klyrow-hidden-operation-count"] == sum(
        1 for _method, _path, include, _name in rows if not include
    )
    assert schema["x-klyrow-runtime-operation-sha256"] == runtime_route_fingerprint(
        rows
    )
    assert any(
        method == "post" and path == "/v1/email/send" and include is False
        for method, path, include, _name in rows
    )
    assert (
        schema["x-klyrow-runtime-operation-count"] > schema["x-klyrow-operation-count"]
    )


def test_security_schemes_and_origin_boundaries_are_explicit():
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert set(schemes) >= {
        "bearerAuth",
        "browserSession",
        "browserCsrf",
        "serviceBearer",
        "metricsBearer",
        "klyrowWebhookSignature",
        "postalSignature",
    }
    assert schemes["browserCsrf"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Klyrow-CSRF",
        "description": (
            "Required session-bound CSRF credential for authenticated "
            "browser mutations."
        ),
    }
    assert schema["paths"]["/auth/session"]["get"]["security"] == [
        {},
        {"browserSession": []},
    ]
    assert schema["paths"]["/v1/me"]["get"]["security"] == [{"bearerAuth": []}]
    assert schema["paths"]["/v1/admin/tenants"]["get"]["x-klyrow-audience"] == "ADMIN"
    activation = schema["paths"]["/v1/admin/delivery/activation"]["get"]
    assert activation["x-klyrow-audience"] == "ADMIN"
    assert activation["security"] == [{"bearerAuth": []}]
    assert (
        schema["paths"]["/t/{kind}/{token}"]["get"]["x-klyrow-audience"] == "TRACKING"
    )
    assert (
        schema["paths"]["/v1/legacy/billing/usage"]["get"]["x-klyrow-audience"]
        == "LEGACY"
    )


def test_pre_session_and_body_token_entrypoints_are_anonymous():
    schema = app.openapi()
    for method, path in BROWSER_ANONYMOUS:
        assert schema["paths"][path][method]["security"] == []
    for method, path in {
        ("get", "/health/live"),
        ("get", "/health/ready"),
        ("post", "/v1/team/invitations/accept"),
    }:
        assert schema["paths"][path][method]["security"] == []


def test_authenticated_browser_mutations_require_session_and_csrf():
    schema = app.openapi()
    for method, path in {
        ("post", "/auth/logout"),
        ("delete", "/auth/sessions/{session_id}"),
        ("patch", "/app/api/onboarding"),
        ("post", "/app/api/onboarding/complete"),
        ("post", "/app/api/mailboxes/{mailbox_id}/send"),
    }:
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"browserSession": [], "browserCsrf": []}]
        assert (
            header_parameter(
                operation,
                "X-Klyrow-CSRF",
            )["required"]
            is True
        )
        assert "csrf_guard" in operation["x-klyrow-enforced-auth-dependencies"]


def test_webhook_schemes_match_runtime_verifiers():
    schema = app.openapi()
    for path in (
        "/v1/webhooks/postal-inbound",
        "/v1/webhooks/postal-native",
    ):
        assert schema["paths"][path]["post"]["security"] == [{"postalSignature": []}]
    assert schema["paths"]["/v1/webhooks/postal"]["post"]["security"] == [
        {"klyrowWebhookSignature": []}
    ]


def test_internal_security_metadata_matches_runtime_authenticators():
    schema = app.openapi()
    assert schema["paths"]["/metrics"]["get"]["security"] == [{"metricsBearer": []}]
    beyvra = schema["paths"]["/v1/internal/email/beyvra/send"]["post"]
    assert beyvra["security"] == [{"serviceBearer": []}]
    assert "beyvra_service_auth" in beyvra["x-klyrow-enforced-auth-dependencies"]

    general = schema["paths"]["/v1/internal/email/send"]["post"]
    assert general["security"] == [{"bearerAuth": []}]
    assert "auth" in general["x-klyrow-enforced-auth-dependencies"]

    system = schema["paths"]["/v1/system/capabilities"]["get"]
    assert system["security"] == [{"bearerAuth": []}]
    assert "auth" in system["x-klyrow-enforced-auth-dependencies"]


def test_runtime_idempotency_registry_is_complete_and_non_circular():
    discovered = runtime_idempotency_routes(app)
    assert discovered == CLASSIFIED_IDEMPOTENCY
    assert CLASSIFIED_IDEMPOTENCY == (REQUIRED_IDEMPOTENCY | OPTIONAL_IDEMPOTENCY)
    assert REQUIRED_IDEMPOTENCY == (DURABLE_IDEMPOTENCY | NON_ATOMIC_ITEM_IDEMPOTENCY)


def test_durable_mutations_require_the_idempotency_header():
    schema = app.openapi()
    for method, path in DURABLE_IDEMPOTENCY:
        if path not in schema["paths"]:
            continue
        operation = schema["paths"][path][method]
        assert operation["x-durable-idempotency"] is True
        assert operation["x-idempotency-model"] == "REQUEST_SCOPED_ATOMIC"
        assert operation["x-idempotency-required"] is True
        assert (
            header_parameter(
                operation,
                "Idempotency-Key",
            )["required"]
            is True
        )


def test_non_atomic_bulk_idempotency_is_truthfully_described():
    schema = app.openapi()
    assert NON_ATOMIC_ITEM_IDEMPOTENCY == {("post", "/v1/email/bulk")}
    operation = schema["paths"]["/v1/email/bulk"]["post"]
    assert operation["x-durable-idempotency"] is False
    assert operation["x-idempotency-model"] == "ITEM_SCOPED_NON_ATOMIC"
    assert operation["x-idempotency-required"] is True
    assert (
        header_parameter(
            operation,
            "Idempotency-Key",
        )["required"]
        is True
    )
    assert ("post", "/v1/email/bulk") not in DURABLE_IDEMPOTENCY


def test_optional_invoice_idempotency_remains_optional_and_explicit():
    schema = app.openapi()
    assert OPTIONAL_IDEMPOTENCY == {
        ("post", "/v1/billing/invoices"),
        ("post", "/v1/events"),
        ("post", "/v1/events/batch"),
        ("post", "/v1/profile-imports"),
        ("post", "/v1/profile-exports"),
    }
    operation = schema["paths"]["/v1/billing/invoices"]["post"]
    assert operation["x-durable-idempotency"] is True
    assert operation["x-idempotency-model"] == "OPTIONAL_REQUEST_SCOPED"
    assert operation["x-idempotency-required"] is False
    assert (
        header_parameter(
            operation,
            "Idempotency-Key",
        )["required"]
        is False
    )


def test_hidden_compatibility_send_is_in_runtime_idempotency_authority():
    schema = app.openapi()
    assert "/v1/email/send" not in schema["paths"]
    assert schema["x-klyrow-hidden-idempotency-routes"] == [
        {
            "method": "POST",
            "path": "/v1/email/send",
            "required": True,
            "model": "REQUEST_SCOPED_ATOMIC",
        }
    ]


def test_schema_generation_is_cached_and_deterministic():
    first = app.openapi()
    second = app.openapi()
    assert first is second
    assert first["x-klyrow-audience-counts"] == {
        "ADMIN": 22,
        "BROWSER_BFF": 50,
        "INTERNAL": 48,
        "LEGACY": 1,
        "PUBLIC": 238,
        "TRACKING": 6,
        "WEBHOOK": 3,
    }
