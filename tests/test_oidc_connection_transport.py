from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.gateway.app import auth_bff


def request():
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/auth/callback",
            "server": ("app.klyrow.com", 443),
            "headers": [(b"host", b"app.klyrow.com")],
            "query_string": b"",
        }
    )


def test_code_exchange_uses_canonical_keycloak_without_proxy_or_redirect(monkeypatch):
    monkeypatch.setenv("KLYROW_OIDC_ISSUER", auth_bff.ISSUER)
    monkeypatch.setenv("KLYROW_OIDC_REDIRECT_URI", "https://app.klyrow.com/auth/callback")
    monkeypatch.delenv("KLYROW_OIDC_CLIENT_SECRET_FILE", raising=False)
    response = Mock()
    response.json.return_value = {"id_token": "synthetic-id-token"}
    post = Mock(return_value=response)
    monkeypatch.setattr(auth_bff.httpx, "post", post)
    result = auth_bff._exchange_code("synthetic-code", "synthetic-verifier", request())
    assert result == {"id_token": "synthetic-id-token"}
    assert post.call_args.args == (auth_bff.ISSUER + "/protocol/openid-connect/token",)
    assert post.call_args.kwargs["trust_env"] is False
    assert post.call_args.kwargs["follow_redirects"] is False
    assert post.call_args.kwargs["data"]["redirect_uri"] == "https://app.klyrow.com/auth/callback"
    assert post.call_args.kwargs["data"]["code_verifier"] == "synthetic-verifier"


@pytest.mark.parametrize("payload", [[], None, 1, "not-an-object"])
def test_malformed_keycloak_response_becomes_sanitized_gateway_error(monkeypatch, payload):
    monkeypatch.setenv("KLYROW_OIDC_ISSUER", auth_bff.ISSUER)
    monkeypatch.delenv("KLYROW_OIDC_CLIENT_SECRET_FILE", raising=False)
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr(auth_bff.httpx, "post", Mock(return_value=response))
    with pytest.raises(HTTPException) as raised:
        auth_bff._exchange_code("synthetic-code", "synthetic-verifier", request())
    assert raised.value.status_code == 502
    assert raised.value.detail == "oidc_token_exchange_failed"


def test_invalid_json_and_token_endpoint_errors_are_sanitized(monkeypatch):
    monkeypatch.setenv("KLYROW_OIDC_ISSUER", auth_bff.ISSUER)
    monkeypatch.delenv("KLYROW_OIDC_CLIENT_SECRET_FILE", raising=False)
    for error in (
        ValueError("private upstream response"),
        httpx.ConnectError("private network detail"),
    ):
        monkeypatch.setattr(auth_bff.httpx, "post", Mock(side_effect=error))
        with pytest.raises(HTTPException) as raised:
            auth_bff._exchange_code("synthetic-code", "synthetic-verifier", request())
        assert raised.value.detail == "oidc_token_exchange_failed"


def test_refresh_uses_the_same_bounded_token_transport(monkeypatch):
    response = Mock()
    response.json.return_value = {"access_token": "synthetic-access"}
    post = Mock(return_value=response)
    monkeypatch.setattr(auth_bff.httpx, "post", post)
    auth_bff._post_oidc_token({"grant_type": "refresh_token", "refresh_token": "synthetic-refresh"})
    assert post.call_args.kwargs["trust_env"] is False
    assert post.call_args.kwargs["follow_redirects"] is False
