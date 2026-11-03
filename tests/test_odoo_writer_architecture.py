"""Source and packaging guardrails; live ACL/firewall tests remain separate."""

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def forbidden_runtime_reference(value):
    normalized = value.lower()
    return (
        bool(re.search(r"odoo.*(?:password|api_key|secret|token|database_url)", normalized))
        or "/jsonrpc" in normalized
        or "/json/2/" in normalized
        or "xmlrpc.client" in normalized
        or "klyrow_mail_odoo" in normalized
    )


def test_gateway_contains_no_odoo_transport_or_writer_credential_reference():
    violations = []
    for path in (ROOT / "apps/gateway/app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
            elif isinstance(node, ast.ImportFrom):
                value = node.module or ""
            elif isinstance(node, ast.Import):
                value = " ".join(alias.name for alias in node.names)
            else:
                continue
            if forbidden_runtime_reference(value):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_gateway_image_does_not_copy_middleware_integration():
    dockerfile = (ROOT / "apps/gateway/Dockerfile").read_text()
    copies = [line for line in dockerfile.splitlines() if line.startswith(("COPY ", "ADD "))]
    assert "COPY apps/gateway/app ./app" in copies
    assert all("integrations/" not in line for line in copies)
    assert all(not re.match(r"(?:COPY|ADD)\s+(?:\.|\./|/|\*)\s", line) for line in copies)


def test_klyrow_and_telemetry_services_have_no_odoo_credential_mounts():
    files = sorted(set(ROOT.glob("*compose*.y*ml")) | set((ROOT / "deploy").glob("*compose*.y*ml")))
    assert files
    for path in files:
        document = yaml.safe_load(path.read_text())
        for name, service in document.get("services", {}).items():
            # These files deploy Klyrow components, never the separately
            # governed Middleware Odoo worker.
            for secret in service.get("secrets", []):
                source = secret if isinstance(secret, str) else secret["source"]
                assert "odoo" not in source.lower(), (path.name, name, source)
            environment = service.get("environment", {})
            names = (
                environment
                if isinstance(environment, dict)
                else [value.split("=", 1)[0] for value in environment]
            )
            assert not any(forbidden_runtime_reference(key) for key in names), (path.name, name)


def test_guard_detects_writer_protocols_and_credentials_but_allows_routing_metadata():
    for value in (
        "KLYROW_ODOO_API_KEY_FILE",
        "/json/2/res.partner/write",
        "/jsonrpc",
        "xmlrpc.client",
        "app.workers.klyrow_mail_odoo",
    ):
        assert forbidden_runtime_reference(value)
    for value in ("odoo_reference", "ODOO", "odoo_helpdesk"):
        assert not forbidden_runtime_reference(value)
