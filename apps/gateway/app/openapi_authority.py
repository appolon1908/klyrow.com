"""Canonical audience, authentication, and idempotency authority for the API."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Iterable

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
MUTATION_METHODS = {"post", "put", "patch", "delete"}
AUDIENCES = {"PUBLIC", "BROWSER_BFF", "INTERNAL", "ADMIN", "WEBHOOK", "TRACKING", "LEGACY"}

PUBLIC_ANONYMOUS = {
    ("get", "/health"),
    ("get", "/healthz"),
    ("get", "/health/live"),
    ("get", "/health/ready"),
    ("get", "/readyz"),
    ("get", "/readiness"),
    ("get", "/capabilities"),
    ("get", "/dependencies"),
    ("get", "/version"),
    ("get", "/v1/health"),
    ("get", "/v1/auth/oidc/config"),
    ("post", "/v1/auth/login"),
    ("post", "/v1/auth/forgot-password"),
    ("post", "/v1/auth/reset-password"),
    ("post", "/v1/team/invitations/accept"),
}

BROWSER_ANONYMOUS = {
    ("get", "/auth/login"),
    ("get", "/auth/signup"),
    ("get", "/auth/google"),
    ("get", "/auth/callback"),
    ("post", "/auth/actions/recover"),
    ("post", "/auth/actions/update-password"),
    ("post", "/auth/actions/verify-email"),
    ("post", "/auth/actions/invitation"),
}

WEBHOOK_PATHS = {
    "/v1/webhooks/postal",
    "/v1/webhooks/postal-inbound",
    "/v1/webhooks/postal-native",
}
POSTAL_SIGNATURE_PATHS = {
    "/v1/webhooks/postal-inbound",
    "/v1/webhooks/postal-native",
}
DEDICATED_SERVICE_PATHS = {
    "/v1/internal/integrations/alertmanager/events",
    "/v1/internal/integrations/kpis/snapshots",
    "/v1/internal/integrations/odoo/health",
    "/v1/internal/integrations/odoo/checkpoints",
    "/v1/internal/integrations/observability/contract",
    "/v1/internal/email/beyvra/send",
}

# Every route that receives Idempotency-Key must be classified exactly once.
# The hidden compatibility send remains in the runtime inventory even though it
# is intentionally omitted from generated OpenAPI clients.
DURABLE_IDEMPOTENCY = {
    ("post", "/v1/messages"),
    ("post", "/v1/email/messages"),
    ("post", "/v1/email/send"),
    ("post", "/v1/internal/email/send"),
    ("post", "/v1/internal/email/communications/messages"),
    ("post", "/v1/internal/email/beyvra/send"),
    ("post", "/app/api/email/send"),
    ("post", "/app/api/mailboxes/{mailbox_id}/send"),
    ("post", "/v1/campaigns"),
    ("post", "/v1/messages/{message_id}/cancel"),
    ("post", "/v1/campaigns/{campaign_id}/schedule"),
    ("post", "/v1/campaigns/{campaign_id}/pause"),
    ("post", "/v1/campaigns/{campaign_id}/resume"),
    ("post", "/v1/campaigns/{campaign_id}/cancel"),
    ("post", "/v1/operations/{operation_id}/cancel"),
    ("post", "/v1/operations/{operation_id}/reconcile"),
    ("post", "/v1/integrations/mautic/operations/{operation_id}/reconcile"),
    ("post", "/v1/integrations/mautic/commands"),
    ("post", "/v1/commands"),
}
NON_ATOMIC_ITEM_IDEMPOTENCY = {
    ("post", "/v1/email/bulk"),
}
OPTIONAL_ITEM_IDEMPOTENCY = {("post", "/v1/events/batch")}
OPTIONAL_IDEMPOTENCY = {
    ("post", "/v1/events"),
    ("post", "/v1/profile-imports"),
    ("post", "/v1/profile-exports"),
    *OPTIONAL_ITEM_IDEMPOTENCY,
    ("post", "/v1/billing/invoices"),
}
REQUIRED_IDEMPOTENCY = DURABLE_IDEMPOTENCY | NON_ATOMIC_ITEM_IDEMPOTENCY
CLASSIFIED_IDEMPOTENCY = REQUIRED_IDEMPOTENCY | OPTIONAL_IDEMPOTENCY

RECOGNIZED_AUTH_DEPENDENCIES = {
    "auth": "bearerAuth",
    "require_observability_read": "serviceBearer",
    "require_observability_write": "serviceBearer",
    "beyvra_service_auth": "serviceBearer",
    "browser_context": "browserSession",
    "csrf_guard": "browserCsrf",
}


def operation_audience(path: str) -> str:
    if path == "/v1/email/messages" or path.startswith("/v1/email/messages/"):
        return "INTERNAL"
    if path.startswith(("/auth/", "/app/")):
        return "BROWSER_BFF"
    if path == "/metrics" or path.startswith(("/internal/v1/", "/v1/internal/", "/v1/system/")):
        return "INTERNAL"
    if path.startswith("/v1/admin/"):
        return "ADMIN"
    if path in WEBHOOK_PATHS:
        return "WEBHOOK"
    if path.startswith(("/t/", "/v1/tracking/", "/v1/unsubscribe")):
        return "TRACKING"
    if path.startswith("/v1/legacy/"):
        return "LEGACY"
    return "PUBLIC"


def operation_auth(
    method: str, path: str, audience: str
) -> tuple[list[dict[str, list[str]]], str]:
    if audience == "BROWSER_BFF":
        if (method, path) in BROWSER_ANONYMOUS:
            return [], "OIDC_PRE_SESSION_OR_SIGNED_BROWSER_ACTION"
        if path == "/auth/session" and method == "get":
            return [{}, {"browserSession": []}], "OPTIONAL_BROWSER_SESSION_COOKIE"
        if method in MUTATION_METHODS:
            return [
                {"browserSession": [], "browserCsrf": []}
            ], "BROWSER_SESSION_COOKIE_AND_REQUIRED_CSRF_HEADER"
        return [{"browserSession": []}], "BROWSER_SESSION_COOKIE"
    if audience == "INTERNAL":
        if path == "/metrics":
            return [{"metricsBearer": []}], "METRICS_BEARER_ON_PRIVATE_ROUTE"
        if path in DEDICATED_SERVICE_PATHS:
            return [{"serviceBearer": []}], "DEDICATED_SERVICE_BEARER_ON_PRIVATE_ROUTE"
        return [{"bearerAuth": []}], "BEARER_JWT_OR_API_KEY_ON_PRIVATE_ROUTE"
    if audience == "ADMIN":
        return [{"bearerAuth": []}], "EXACT_PLATFORM_OWNER_OIDC_WITH_VERIFIED_EMAIL_AND_FRESH_MFA"
    if audience == "LEGACY":
        return [{"bearerAuth": []}], "BEARER_JWT_OR_API_KEY_WITH_ROLE"
    if audience == "WEBHOOK":
        if path in POSTAL_SIGNATURE_PATHS:
            return [{"postalSignature": []}], "POSTAL_RSA_SHA256_SIGNATURE_AND_TIMESTAMP"
        return [{"klyrowWebhookSignature": []}], "HMAC_SHA256_SIGNATURE_TIMESTAMP_AND_REPLAY_ID"
    if audience == "TRACKING":
        if path.startswith("/t/"):
            return [], "SIGNED_SINGLE_USE_PATH_TOKEN"
        if path == "/v1/unsubscribe" and method == "post":
            return [], "SIGNED_UNSUBSCRIBE_TOKEN"
        return [{"bearerAuth": []}], "BEARER_JWT_OR_API_KEY"
    if (method, path) in PUBLIC_ANONYMOUS:
        return [], "NONE_OR_BODY_BOUND_SINGLE_USE_TOKEN"
    return [{"bearerAuth": []}], "BEARER_JWT_OR_API_KEY"


def _header_parameter(operation: dict[str, Any], name: str) -> dict[str, Any] | None:
    normalized = name.lower()
    return next(
        (
            item
            for item in operation.get("parameters", [])
            if item.get("in") == "header"
            and str(item.get("name", "")).lower() == normalized
        ),
        None,
    )


def _require_header_parameter(
    operation: dict[str, Any],
    *,
    method: str,
    path: str,
    name: str,
) -> None:
    parameter = _header_parameter(operation, name)
    if parameter is None:
        raise RuntimeError(
            f"required runtime header is absent from OpenAPI: "
            f"{method.upper()} {path} {name}"
        )
    parameter["required"] = True


def _walk_dependencies(dependant: Any) -> Iterable[Any]:
    for dependency in getattr(dependant, "dependencies", ()):
        yield dependency
        yield from _walk_dependencies(dependency)


def _api_route_contexts(app: FastAPI) -> list[RouteContext]:
    """Flatten included routers using FastAPI's effective route contexts."""

    return [
        route_context
        for route_context in iter_route_contexts(app.routes)
        if isinstance(route_context.original_route, APIRoute)
    ]


def _dependency_names(route: RouteContext) -> set[str]:
    names: set[str] = set()
    for dependency in _walk_dependencies(route.dependant):
        call = getattr(dependency, "call", None)
        name = getattr(call, "__name__", None)
        if name:
            names.add(str(name))
    return names


def _dependency_headers(route: RouteContext) -> set[str]:
    aliases: set[str] = set()

    def collect(dependant: Any) -> None:
        for field in getattr(dependant, "header_params", ()):
            aliases.add(str(getattr(field, "alias", "")).lower())
        for child in getattr(dependant, "dependencies", ()):
            collect(child)

    collect(route.dependant)
    return aliases


def runtime_routes(app: FastAPI) -> list[tuple[str, str, bool, str]]:
    """Return every effective APIRoute operation, including hidden routes."""

    rows: list[tuple[str, str, bool, str]] = []
    for route in _api_route_contexts(app):
        path = route.path
        if path is None:
            raise RuntimeError("effective APIRoute is missing its path")
        for method in sorted(route.methods or ()):
            normalized = method.lower()
            if normalized in HTTP_METHODS:
                rows.append(
                    (
                        normalized,
                        path,
                        bool(route.include_in_schema),
                        str(route.name),
                    )
                )
    return sorted(rows)


def runtime_route_fingerprint(
    rows: Iterable[tuple[str, str, bool, str]]
) -> str:
    canonical = "\n".join(
        f"{method.upper()} {path}\tinclude_in_schema={str(include).lower()}\tname={name}"
        for method, path, include, name in rows
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def runtime_idempotency_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Discover every effective route that accepts Idempotency-Key."""

    operations: set[tuple[str, str]] = set()
    for route in _api_route_contexts(app):
        if "idempotency-key" not in _dependency_headers(route):
            continue
        path = route.path
        if path is None:
            raise RuntimeError("idempotent APIRoute is missing its path")
        for method in route.methods or ():
            normalized = method.lower()
            if normalized in HTTP_METHODS:
                operations.add((normalized, path))
    return operations


def _route_index(app: FastAPI) -> dict[tuple[str, str], list[RouteContext]]:
    index: dict[tuple[str, str], list[RouteContext]] = {}
    for route in _api_route_contexts(app):
        path = route.path
        if path is None:
            raise RuntimeError("effective APIRoute is missing its path")
        for method in route.methods or ():
            normalized = method.lower()
            if normalized in HTTP_METHODS:
                index.setdefault((normalized, path), []).append(route)
    return index


def _security_scheme_names(
    security: list[dict[str, list[str]]],
) -> set[str]:
    return {name for requirement in security for name in requirement}


def _validate_dependency_security(
    routes: list[RouteContext],
    *,
    method: str,
    path: str,
    security: list[dict[str, list[str]]],
) -> list[str]:
    dependencies = set().union(*(_dependency_names(route) for route in routes))
    schemes = _security_scheme_names(security)
    enforced = sorted(
        dependency
        for dependency in dependencies
        if dependency in RECOGNIZED_AUTH_DEPENDENCIES
    )
    for dependency in enforced:
        required_scheme = RECOGNIZED_AUTH_DEPENDENCIES[dependency]
        # The observability guard narrows its underlying auth dependency to
        # service identities with an explicit observability grant.
        if dependency == "auth" and dependencies.intersection({
            "require_observability_read", "require_observability_write",
        }):
            required_scheme = "serviceBearer"
        if required_scheme not in schemes:
            raise RuntimeError(
                f"OpenAPI auth mismatch for {method.upper()} {path}: "
                f"dependency {dependency} requires {required_scheme}"
            )
    return enforced


def _validate_idempotency_registry(app: FastAPI) -> set[tuple[str, str]]:
    discovered = runtime_idempotency_routes(app)
    if discovered != CLASSIFIED_IDEMPOTENCY:
        unclassified = sorted(discovered - CLASSIFIED_IDEMPOTENCY)
        stale = sorted(CLASSIFIED_IDEMPOTENCY - discovered)
        raise RuntimeError(
            "runtime idempotency registry mismatch: "
            f"unclassified={unclassified!r} stale={stale!r}"
        )
    return discovered


def build_openapi(app: FastAPI) -> dict[str, Any]:
    runtime_idempotency = _validate_idempotency_registry(app)
    route_index = _route_index(app)
    runtime_rows = runtime_routes(app)

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    components = schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes.update(
        {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT or tenant API key",
                "description": "Tenant-bound JWT or API key validated by the gateway.",
            },
            "browserSession": {
                "type": "apiKey",
                "in": "cookie",
                "name": "__Host-klyrow_session",
                "description": "HttpOnly same-origin browser session.",
            },
            "browserCsrf": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Klyrow-CSRF",
                "description": "Required session-bound CSRF credential for authenticated browser mutations.",
            },
            "serviceBearer": {
                "type": "http",
                "scheme": "bearer",
                "description": "Dedicated service credential accepted only by handlers that use the service authenticator.",
            },
            "metricsBearer": {
                "type": "http",
                "scheme": "bearer",
                "description": "Dedicated metrics credential; the public edge returns 404 for this route.",
            },
            "klyrowWebhookSignature": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Klyrow-Signature",
                "description": "HMAC-SHA256 signature bound to timestamp, replay ID, and raw body.",
            },
            "postalSignature": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Postal-Signature-256",
                "description": "Postal RSA-SHA256 signature over the raw callback body.",
            },
        }
    )

    counts: Counter[str] = Counter()
    operation_count = 0
    documented_operations: set[tuple[str, str]] = set()
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_key = (method, path)
            documented_operations.add(operation_key)
            routes = route_index.get(operation_key)
            if not routes:
                raise RuntimeError(
                    f"OpenAPI operation has no canonical APIRoute: "
                    f"{method.upper()} {path}"
                )

            audience = operation_audience(path)
            if audience not in AUDIENCES:
                raise RuntimeError(
                    f"unknown OpenAPI audience for {method.upper()} {path}"
                )
            security, auth_model = operation_auth(method, path, audience)
            enforced_dependencies = _validate_dependency_security(
                routes,
                method=method,
                path=path,
                security=security,
            )
            operation["security"] = security
            operation["x-klyrow-audience"] = audience
            operation["x-klyrow-auth-model"] = auth_model
            operation["x-klyrow-enforced-auth-dependencies"] = enforced_dependencies

            if (
                audience == "BROWSER_BFF"
                and method in MUTATION_METHODS
                and operation_key not in BROWSER_ANONYMOUS
            ):
                _require_header_parameter(
                    operation,
                    method=method,
                    path=path,
                    name="X-Klyrow-CSRF",
                )

            if operation_key in REQUIRED_IDEMPOTENCY:
                _require_header_parameter(
                    operation,
                    method=method,
                    path=path,
                    name="Idempotency-Key",
                )
                operation["x-idempotency-required"] = True
            elif operation_key in OPTIONAL_IDEMPOTENCY:
                if _header_parameter(operation, "Idempotency-Key") is None:
                    raise RuntimeError(
                        f"optional idempotency header is absent from OpenAPI: "
                        f"{method.upper()} {path}"
                    )
                operation["x-idempotency-required"] = False

            if operation_key in DURABLE_IDEMPOTENCY:
                operation["x-durable-idempotency"] = True
                operation["x-idempotency-model"] = "REQUEST_SCOPED_ATOMIC"
            elif operation_key in NON_ATOMIC_ITEM_IDEMPOTENCY:
                operation["x-durable-idempotency"] = False
                operation["x-idempotency-model"] = "ITEM_SCOPED_NON_ATOMIC"
            elif operation_key in OPTIONAL_ITEM_IDEMPOTENCY:
                operation["x-durable-idempotency"] = False
                operation["x-idempotency-model"] = "OPTIONAL_ITEM_SCOPED_NON_ATOMIC"
            elif operation_key in OPTIONAL_IDEMPOTENCY:
                operation["x-durable-idempotency"] = True
                operation["x-idempotency-model"] = "OPTIONAL_REQUEST_SCOPED"

            counts[audience] += 1
            operation_count += 1

    documented_idempotency = {
        operation
        for operation in runtime_idempotency
        if operation in documented_operations
    }
    hidden_idempotency = sorted(runtime_idempotency - documented_operations)
    expected_documented = CLASSIFIED_IDEMPOTENCY - set(hidden_idempotency)
    if documented_idempotency != expected_documented:
        raise RuntimeError(
            "documented idempotency authority mismatch: "
            f"actual={sorted(documented_idempotency)!r} "
            f"expected={sorted(expected_documented)!r}"
        )

    schema["x-klyrow-operation-count"] = operation_count
    schema["x-klyrow-audience-counts"] = {
        name: counts[name] for name in sorted(AUDIENCES)
    }
    schema["x-klyrow-runtime-operation-count"] = len(runtime_rows)
    schema["x-klyrow-hidden-operation-count"] = sum(
        1 for _method, _path, include, _name in runtime_rows if not include
    )
    schema["x-klyrow-runtime-operation-sha256"] = runtime_route_fingerprint(
        runtime_rows
    )
    schema["x-klyrow-hidden-idempotency-routes"] = [
        {
            "method": method.upper(),
            "path": path,
            "required": operation in REQUIRED_IDEMPOTENCY,
            "model": (
                "REQUEST_SCOPED_ATOMIC"
                if operation in DURABLE_IDEMPOTENCY
                else "ITEM_SCOPED_NON_ATOMIC"
                if operation in NON_ATOMIC_ITEM_IDEMPOTENCY
                else "OPTIONAL_REQUEST_SCOPED"
            ),
        }
        for operation in hidden_idempotency
        for method, path in (operation,)
    ]
    return schema


def install_openapi_authority(app: FastAPI) -> None:
    def canonical_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi_schema = None
    app.openapi = canonical_openapi
