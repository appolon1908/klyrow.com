from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from apps.gateway.app.platform import app  # Load the production composition root first.
from apps.gateway.app.openapi_authority import operation_audience, operation_auth
from apps.gateway.app.production_api import health_live, health_ready


@pytest.mark.parametrize("path", [
    "/internal/v1/alerts/alertmanager",
    "/internal/v1/kpis", "/internal/v1/odoo/sync-status",
    "/v1/internal/email/send", "/metrics",
])
def test_private_api_namespace_never_classified_public(path):
    assert operation_audience(path) == "INTERNAL"
    security, _ = operation_auth("post", path, "INTERNAL")
    assert security and all(security)


def test_similar_public_path_is_not_accidentally_private():
    assert operation_audience("/v1/contacts") == "PUBLIC"
    assert operation_audience("/internal/v10/example") == "PUBLIC"


def test_readiness_fails_safely_when_database_cannot_accept_work():
    session = Mock()
    session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("sensitive connection information"))
    with pytest.raises(HTTPException) as rejected:
        health_ready(session)
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "database_unavailable"
    assert "sensitive" not in rejected.value.detail
    assert health_live() == {"status": "live"}


def test_readiness_only_requires_local_database():
    session = Mock()
    assert health_ready(session) == {"status": "ready", "database": "ok"}
    session.execute.assert_called_once()
