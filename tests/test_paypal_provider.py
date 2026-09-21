import base64
import json
import zlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from apps.gateway.app.paypal_provider import (
    PayPalOrdersAdapter,
    PayPalProviderError,
    verify_paypal_signature,
)


def test_paypal_order_uses_oauth_server_amount_and_idempotency():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            assert request.headers["authorization"].startswith("Basic ")
            assert request.content == b"grant_type=client_credentials"
            return httpx.Response(200, json={"access_token": "access-token", "token_type": "Bearer"})
        assert request.url.path == "/v2/checkout/orders"
        assert request.headers["paypal-request-id"] == "order-key"
        payload = json.loads(request.content)
        assert payload["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "12.34"}
        assert payload["purchase_units"][0]["reference_id"] == "attempt-1"
        assert payload["purchase_units"][0]["custom_id"] == "tenant-1"
        assert payload["purchase_units"][0]["invoice_id"] == "INV-1"
        return httpx.Response(201, json={
            "id": "5O190127TN364715T",
            "status": "PAYER_ACTION_REQUIRED",
            "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T"}],
        })

    adapter = PayPalOrdersAdapter(
        client_id="client-id",
        client_secret="client-secret",
        api_base_url="https://api-m.sandbox.paypal.com",
        environment="sandbox",
        transport=httpx.MockTransport(handler),
    )
    result = adapter.create_order(
        payment_attempt_id="attempt-1",
        invoice_id="invoice-1",
        invoice_number="INV-1",
        tenant_id="tenant-1",
        amount_minor=1234,
        currency="USD",
        idempotency_key="order-key",
        return_url="https://app.klyrow.test/return",
        cancel_url="https://app.klyrow.test/cancel",
    )
    assert result.order_id == "5O190127TN364715T"
    assert result.approval_url.startswith("https://www.sandbox.paypal.com/")
    assert len(requests) == 2


def test_paypal_rejects_wrong_approval_host():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "access-token", "token_type": "Bearer"})
        return httpx.Response(201, json={
            "id": "ORDER1",
            "links": [{"rel": "approve", "href": "https://evil.example/checkout?token=ORDER1"}],
        })

    adapter = PayPalOrdersAdapter(
        client_id="client-id",
        client_secret="client-secret",
        api_base_url="https://api-m.sandbox.paypal.com",
        environment="sandbox",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PayPalProviderError, match="paypal_order_invalid_response"):
        adapter.create_order(
            payment_attempt_id="attempt",
            invoice_id="invoice",
            invoice_number="INV",
            tenant_id="tenant",
            amount_minor=100,
            currency="USD",
            idempotency_key="key",
            return_url="https://app.klyrow.test/return",
            cancel_url="https://app.klyrow.test/cancel",
        )


def _certificate(now):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.sandbox.paypal.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    return key, pem


def test_paypal_webhook_signature_verifies_raw_body_and_cert_host():
    now = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
    body = b'{"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"CAP-1"}}'
    transmission_id = "transmission-1"
    transmission_time = now.isoformat().replace("+00:00", "Z")
    webhook_id = "webhook-1"
    crc = zlib.crc32(body) & 0xFFFFFFFF
    message = f"{transmission_id}|{transmission_time}|{webhook_id}|{crc}".encode()
    key, pem = _certificate(now)
    signature = base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.sandbox.paypal.com"
        return httpx.Response(200, content=pem)

    headers = {
        "PAYPAL-TRANSMISSION-ID": transmission_id,
        "PAYPAL-TRANSMISSION-TIME": transmission_time,
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-TEST",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-TRANSMISSION-SIG": signature,
    }
    verify_paypal_signature(
        body,
        headers,
        webhook_id=webhook_id,
        environment="sandbox",
        transport=httpx.MockTransport(handler),
        now_utc=now,
    )

    with pytest.raises(PayPalProviderError, match="paypal_webhook_signature_invalid"):
        verify_paypal_signature(
            body + b" ",
            headers,
            webhook_id=webhook_id,
            environment="sandbox",
            transport=httpx.MockTransport(handler),
            now_utc=now,
        )


def test_paypal_webhook_rejects_wrong_environment_cert_host():
    now = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
    headers = {
        "PAYPAL-TRANSMISSION-ID": "t",
        "PAYPAL-TRANSMISSION-TIME": now.isoformat().replace("+00:00", "Z"),
        "PAYPAL-CERT-URL": "https://api.paypal.com/v1/notifications/certs/CERT-TEST",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-TRANSMISSION-SIG": base64.b64encode(b"x").decode(),
    }
    with pytest.raises(PayPalProviderError, match="paypal_webhook_cert_url_invalid"):
        verify_paypal_signature(
            b"{}",
            headers,
            webhook_id="webhook",
            environment="sandbox",
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            now_utc=now,
        )
