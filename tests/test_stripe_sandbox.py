import hashlib
import hmac
from unittest.mock import Mock

import httpx
import pytest

from apps.gateway.app.stripe_sandbox import StripeProductionAdapter, StripeSandboxAdapter, StripeWebhookError, verify_stripe_signature


def _signature(secret: str, body: bytes, timestamp: int = 1_700_000_000) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_signature_verification_requires_the_original_bytes():
    body = b'{"id":"evt_test","type":"checkout.session.completed"}'
    assert verify_stripe_signature(body, _signature("whsec_test", body), "whsec_test", now_epoch=1_700_000_010) is None
    with pytest.raises(StripeWebhookError, match="stripe_webhook_signature_invalid"):
        verify_stripe_signature(body + b" ", _signature("whsec_test", body), "whsec_test", now_epoch=1_700_000_010)


def test_checkout_uses_server_amount_and_stable_idempotency_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"id": "cs_test_123", "url": "https://checkout.stripe.com/cs_test_123"})

    adapter = StripeSandboxAdapter("sk_test_fixture", transport=httpx.MockTransport(handler))
    result = adapter.create_checkout(payment_attempt_id="attempt_123", invoice_id="inv_123", tenant_id="tenant_a", amount_minor=1250, currency="USD", idempotency_key="attempt_123", success_url="https://app.example/app/billing/invoices/inv_123", cancel_url="https://app.example/app/billing/invoices/inv_123")
    assert result.session_id == "cs_test_123"
    assert result.checkout_url == "https://checkout.stripe.com/cs_test_123"
    assert captured["headers"]["Idempotency-Key"] == "attempt_123"
    assert "line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D=1250" in captured["body"]
    assert "metadata%5Binvoice_id%5D=inv_123" in captured["body"]


def test_adapter_timeout_does_not_hide_ambiguous_provider_result():
    adapter = StripeSandboxAdapter("sk_test_fixture", transport=httpx.MockTransport(lambda _request: (_ for _ in ()).throw(httpx.ReadTimeout("timeout"))))
    with pytest.raises(StripeWebhookError, match="stripe_checkout_ambiguous"):
        adapter.create_checkout(payment_attempt_id="attempt", invoice_id="inv", tenant_id="tenant", amount_minor=100, currency="USD", idempotency_key="attempt", success_url="https://app.example/success", cancel_url="https://app.example/cancel")


def test_production_adapter_requires_live_key_and_never_uses_sandbox_key():
    with pytest.raises(StripeWebhookError, match="stripe_production_key_required"):
        StripeProductionAdapter("sk_test_fixture")


def test_production_checkout_uses_injected_transport_without_network_call():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json={"id": "cs_live_mock", "url": "https://checkout.stripe.com/cs_live_mock"})
    adapter = StripeProductionAdapter("sk_live_fixture", transport=httpx.MockTransport(handler))
    result = adapter.create_checkout(payment_attempt_id="attempt", invoice_id="inv", tenant_id="tenant", amount_minor=100, currency="USD", idempotency_key="attempt", success_url="https://app.example/success", cancel_url="https://app.example/cancel")
    assert result.session_id == "cs_live_mock"
    assert len(calls) == 1