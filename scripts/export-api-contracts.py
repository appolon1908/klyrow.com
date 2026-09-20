#!/usr/bin/env python3
"""Export the composed API without starting workers or connecting to a database.

Run with --check in CI. Secret inventory contains names and source locations,
never configured values. Existing API behavior remains the authority during
the contract-first migration; exports do not invent future endpoints.
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def secret_references() -> list[dict]:
    rows = set()
    paths = {ROOT / ".env.example"}
    paths.update(ROOT.glob("*compose*.y*ml"))
    for directory in ("apps", "integrations", "scripts", "config", "deploy"):
        paths.update((ROOT / directory).rglob("*"))
    for path in sorted(paths):
        if not path.is_file() or (path.suffix not in {".py", ".yaml", ".yml", ".json", ".sh", ".example"}
                                  and path.parent != ROOT / "scripts" and path.name != "Dockerfile"):
            continue
        if any(part in {"node_modules", ".venv", "vendor", "dist"} for part in path.parts):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for name in re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", line):
                if re.search(r"(?:SECRET|PASSWORD|TOKEN|API_KEY|KEYRING|PRIVATE_KEY)", name):
                    rows.add((path.relative_to(ROOT).as_posix(), number, name))
    return [dict(path=path, line=line, name=name) for path, line, name in sorted(rows)]


def source_handlers() -> list[dict]:
    """Include secondary ASGI apps and Middleware slices not mounted by platform."""
    rows = []
    for directory in ("apps", "integrations"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            if any(part in {"node_modules", ".venv", "vendor", "__pycache__"} for part in path.parts):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                        continue
                    if decorator.func.attr not in {"get", "post", "put", "patch", "delete", "head", "options", "websocket"} or not decorator.args:
                        continue
                    arg = decorator.args[0]
                    route = arg.value if isinstance(arg, ast.Constant) else ast.unparse(arg)
                    rows.append(dict(file=path.relative_to(ROOT).as_posix(), line=node.lineno,
                                     method=decorator.func.attr.upper(), declared_path=route, handler=node.name))
    return sorted(rows, key=lambda row: (row["file"], row["line"], row["method"], row["declared_path"]))


def audience_schema(schema: dict, audiences: set[str], title: str) -> dict:
    result = copy.deepcopy(schema)
    result["info"]["title"] = title
    result["paths"] = {
        path: {method: op for method, op in item.items()
               if isinstance(op, dict) and op.get("x-klyrow-audience") in audiences}
        for path, item in schema["paths"].items()
    }
    result["paths"] = {path: item for path, item in result["paths"].items() if item}
    for key in list(result):
        if key.startswith("x-klyrow-"):
            del result[key]
    result["x-klyrow-export-audiences"] = sorted(audiences)
    # Keep only transitively referenced components, including auth schemes.
    needed: dict[str, set[str]] = {}
    def discover(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/"):
                _, _, kind, name = ref.split("/", 3)
                needed.setdefault(kind, set()).add(name)
            for requirement in value.get("security", []):
                needed.setdefault("securitySchemes", set()).update(requirement)
            for child in value.values():
                discover(child)
        elif isinstance(value, list):
            for child in value:
                discover(child)
    discover(result["paths"])
    components = schema.get("components", {})
    while True:
        before = {key: set(value) for key, value in needed.items()}
        for kind, names in before.items():
            for name in names:
                discover(components[kind][name])
        if before == needed:
            break
    result["components"] = {kind: {name: components[kind][name] for name in sorted(names)}
                            for kind, names in sorted(needed.items())}
    return result


def artifacts() -> dict[str, str]:
    # Import in an isolated development configuration, never a caller's live
    # configuration. No startup/lifespan hooks or provider requests are run.
    for name in list(os.environ):
        if name.startswith(("KLYROW_", "CODESTRA_")):
            del os.environ[name]
    os.environ.update(KLYROW_ENV="development", KLYROW_DATABASE_URL="sqlite://",
                      KLYROW_SAFE_MODE="true", LIVE_EMAIL_DELIVERY="false",
                      EXTERNAL_EMAIL_DELIVERY="false", PRODUCTION_PROVIDER_ROUTING="false")
    from apps.gateway.app.platform import app
    from apps.gateway.app.openapi_authority import runtime_routes, operation_audience, operation_auth
    from apps.gateway.app.main import Base
    from fastapi.routing import APIRoute
    schema = app.openapi()
    rows = runtime_routes(app)
    catalog = ["# Current Klyrow API", "", "Generated by `scripts/export-api-contracts.py`; edit handlers, then regenerate.", "",
               "This inventories the composed platform, including hidden compatibility and browser routes. Authentication classifications describe code intent, not a live authorization test.", "",
               "| Method | Path | Audience | Authentication | In OpenAPI | Handler |",
               "| --- | --- | --- | --- | --- | --- |"]
    for method, path, included, handler in rows:
        audience = operation_audience(path)
        _, auth = operation_auth(method, path, audience)
        catalog.append(f"| {method.upper()} | `{path}` | {audience} | {auth} | {str(included).lower()} | `{handler}` |")
    catalog.extend(["", "## Framework routes and mounts", "",
                    "Export configuration is development with sending disabled. `/docs` is disabled by application configuration in production; edge policy must separately restrict schema/documentation and static mounts as required.", "",
                    "| Methods | Path | Route kind |", "| --- | --- | --- |"])
    for route in app.routes:
        if isinstance(route, APIRoute) or not getattr(route, "path", None):
            continue
        methods = ", ".join(sorted(getattr(route, "methods", []) or [])) or "MOUNT"
        catalog.append(f"| {methods} | `{route.path}` | {type(route).__name__} |")
    catalog.extend(["", "Secondary app and source-declared handlers (prefixes may be applied by their composition roots) are in `source-handlers.json`. Infrastructure APIs are inventoried in `docs/architecture/current-state.md`.", ""])
    def encode(value):
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return {
        "docs/api/current-api.md": "\n".join(catalog),
        "docs/api/source-handlers.json": encode(source_handlers()),
        "docs/api/runtime-routes.json": encode([dict(method=m.upper(), path=p, documented=i, handler=h) for m, p, i, h in rows]),
        "docs/architecture/database-inventory.json": encode([
            dict(table=table.name, tenant_scoped="tenant_id" in table.columns,
                 columns=[column.name for column in table.columns])
            for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name)]),
        "docs/security/secret-references.json": encode(secret_references()),
        # JSON is valid YAML 1.2 and avoids a second serialization authority.
        "schemas/openapi/klyrow-public-api.yaml": encode(audience_schema(schema, {"PUBLIC"}, "Klyrow Public API")),
        "schemas/openapi/klyrow-internal-api.yaml": encode(audience_schema(schema, {"INTERNAL", "ADMIN"}, "Klyrow Private API")),
        "schemas/openapi/klyrow-browser-api.yaml": encode(audience_schema(schema, {"BROWSER_BFF"}, "Klyrow Browser BFF")),
        "schemas/openapi/klyrow-callback-api.yaml": encode(audience_schema(schema, {"WEBHOOK", "TRACKING", "LEGACY"}, "Klyrow Callbacks and Compatibility API")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, content in artifacts().items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if stale:
        print("Stale API/inventory artifacts: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
