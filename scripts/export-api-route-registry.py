#!/usr/bin/env python3
"""Generate the source-owned API route registry from the composed FastAPI app."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "docs/api/route-registry.json"


def classification(method: str, path: str, documented: bool) -> str:
    if not documented or path.startswith(("/v1/internal/", "/v1/legacy/")):
        return "COMPATIBILITY"
    return "KEEP"


def pagination(operation: dict) -> dict:
    names = {
        str(parameter.get("name"))
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query"
    }
    return {
        "present": bool(names.intersection({"limit", "offset", "cursor", "page"})),
        "parameters": sorted(names.intersection({"limit", "offset", "cursor", "page", "sort"})),
    }


def build() -> list[dict]:
    for name in list(os.environ):
        if name.startswith(("KLYROW_", "CODESTRA_")):
            del os.environ[name]
    os.environ.update(
        KLYROW_ENV="development",
        KLYROW_DATABASE_URL="sqlite://",
        KLYROW_SAFE_MODE="true",
        LIVE_EMAIL_DELIVERY="false",
        EXTERNAL_EMAIL_DELIVERY="false",
        PRODUCTION_PROVIDER_ROUTING="false",
    )
    from apps.gateway.app.platform import app
    from apps.gateway.app.openapi_authority import operation_audience, operation_auth, runtime_routes

    schema = app.openapi()
    openapi_operations = {
        (method.upper(), path): operation
        for path, item in schema.get("paths", {}).items()
        for method, operation in item.items()
        if isinstance(operation, dict)
    }
    rows = []
    for method, path, documented, handler in runtime_routes(app):
        key = (method.upper(), path)
        audience = operation_audience(path)
        security, auth_model = operation_auth(method, path, audience)
        operation = openapi_operations.get(key, {})
        operation_id = operation.get("operationId") or f"runtime_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '') or 'root'}"
        mutation = method.lower() in {"post", "put", "patch", "delete"}
        rows.append({
            "operation_id": operation_id,
            "method": method.upper(),
            "path": path,
            "handler": handler,
            "audience": audience,
            "namespace": (
                "/app/api/*" if path.startswith("/app/") else
                "/v1/admin/*" if path.startswith("/v1/admin/") else
                "/internal/v1/*" if path.startswith("/internal/v1/") else
                "/v1/internal/*" if path.startswith("/v1/internal/") else
                "/v1/*" if path.startswith("/v1/") else
                "/auth/*" if path.startswith("/auth/") else
                "other"
            ),
            "classification": classification(method, path, documented),
            "documented": documented,
            "authentication": {"security": security, "model": auth_model},
            "csrf": {"required": audience == "BROWSER_BFF" and mutation and (method, path) not in {("post", "/auth/actions/recover"), ("post", "/auth/actions/update-password"), ("post", "/auth/actions/verify-email"), ("post", "/auth/actions/invitation")}},
            "idempotency": {
                "required": bool(operation.get("x-idempotency-required", False)),
                "durable": bool(operation.get("x-durable-idempotency", False)),
                "model": operation.get("x-idempotency-model"),
            },
            "pagination": pagination(operation),
            "response": operation.get("responses", {}),
        })
    return sorted(rows, key=lambda row: (row["path"], row["method"], row["handler"]))


def main() -> int:
    content = json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            print(f"stale route registry: {OUTPUT}", file=sys.stderr)
            return 1
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
