"""Tenant models can be imported before the application composition root."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("first_module", ["tenancy", "auth_bff", "platform_owner", "browser_observability"])
def test_early_tenancy_import_preserves_core_admin_authentication(first_module):
    code = f"""
import importlib
importlib.import_module('apps.gateway.app.{first_module}')
from apps.gateway.app import main
from apps.gateway.app.platform import app
from apps.gateway.app.openapi_authority import _api_route_contexts
from fastapi.testclient import TestClient

def has_core_auth(dependency):
    return dependency.call is main.auth or any(
        has_core_auth(child) for child in dependency.dependencies
    )

routes = [route for route in _api_route_contexts(app)
          if route.path == '/v1/admin/oidc-identities']
assert len(routes) == 1
assert has_core_auth(routes[0].dependant)
response = TestClient(app).post('/v1/admin/oidc-identities', json={{
    'subject': 'untrusted-subject', 'user_id': 'untrusted-user',
    'identity_type': 'KLYROW_ONLY',
}})
assert response.status_code == 401, response.text
observability_path = "/app/api/admin/observability/webmail-postal"
assert len([route for route in _api_route_contexts(app)
            if route.path == observability_path]) == 1
response = TestClient(app).get(observability_path)
assert response.status_code == 401, response.text
"""
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("KLYROW_", "CODESTRA_"))
    }
    env.update(
        KLYROW_ENV="development", KLYROW_DATABASE_URL="sqlite://",
        KLYROW_SAFE_MODE="true", KLYROW_EMBEDDED_WORKERS="false",
        LIVE_EMAIL_DELIVERY="false", EXTERNAL_EMAIL_DELIVERY="false",
        PRODUCTION_PROVIDER_ROUTING="false",
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
