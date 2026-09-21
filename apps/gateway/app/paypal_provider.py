"PayPal Orders v2 boundary for hosted checkout, capture, and webhook verification."

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .billing_config import BillingConfigError, _read_secret_file, load_billing_settings


class PayPalProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class PayPalOrderResult:
    order_id: str
    approval_url: str


@dataclass(frozen=True)
class PayPalCaptureResult:
    order_id: str
    status: str
    capture_id: str | None


def _minor_value(amount_minor: int) -> str:
    if amount_minor <= 0:
        raise PayPalProviderError("paypal_checkout_invalid_amount")
    return f"{Decimal(amount_minor) / Decimal(100):.2f}"


def _official_host(environment: str) -> str:
    return "api-m.sandbox.paypal.com" if environment == "sandbox" else "api-m.paypal.com"


def _approval_host(environment: str) -> str:
    return "www.sandbox.paypal.com" if environment == "sandbox" else "www.paypal.com"


def _cert_host(environment: str) -> str:
    # PayPal webhook cert URLs use the non -m API hostname.
    return "api.sandbox.paypal.com" if environment == "sandbox" else "api.paypal.com"


class PayPalOrdersAdapter:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        api_base_url: str,
        environment: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(api_base_url)
        if (
            environment not in {"sandbox", "production"}
            or parsed.scheme != "https"
            or parsed.hostname != _official_host(environment)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise PayPalProviderError("paypal_api_base_url_invalid")
        if not client_id or not client_secret:
            raise PayPalProviderError("paypal_credentials_missing")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = api_base_url.rstrip("/")
        self._environment = environment
        self._transport = transport

    def _token(self) -> str:
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=httpx.Timeout(10.0, connect=3.0),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    "/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                    headers={"Accept": "application/json", "Accept-Language": "en_US"},
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.TimeoutException, httpx.TransportError):
            raise PayPalProviderError("paypal_auth_ambiguous") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise PayPalProviderError("paypal_auth_unavailable") from None
        token = data.get("access_token") if isinstance(data, dict) else None
        token_type = str(data.get("token_type") or "").lower() if isinstance(data, dict) else ""
        if not isinstance(token, str) or not token or token_type != "bearer":
            raise PayPalProviderError("paypal_auth_invalid_response")
        return token

    def create_order(
        self,
        *,
        payment_attempt_id: str,
        invoice_id: str,
        invoice_number: str,
        tenant_id: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        return_url: str,
        cancel_url: str,
    ) -> PayPalOrderResult:
        if not currency.isupper() or len(currency) != 3:
            raise PayPalProviderError("paypal_checkout_invalid_currency")
        token = self._token()
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": payment_attempt_id,
                "invoice_id": invoice_number,
                "custom_id": tenant_id,
                "amount": {"currency_code": currency, "value": _minor_value(amount_minor)},
            }],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                        "user_action": "PAY_NOW",
                    }
                }
            },
        }
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=httpx.Timeout(10.0, connect=3.0),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    "/v2/checkout/orders",
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                        "PayPal-Request-Id": idempotency_key,
                        "Prefer": "return=representation",
                    },
                )
                if response.status_code >= 500:
                    raise PayPalProviderError("paypal_order_ambiguous")
                response.raise_for_status()
                data = response.json()
        except PayPalProviderError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise PayPalProviderError("paypal_order_ambiguous") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise PayPalProviderError("paypal_order_unavailable") from None

        order_id = data.get("id") if isinstance(data, dict) else None
        links = data.get("links") if isinstance(data, dict) else None
        approval_url = None
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict) and link.get("rel") in {"approve", "payer-action"}:
                    approval_url = link.get("href")
                    break
        parsed = urlsplit(approval_url) if isinstance(approval_url, str) else None
        if (
            not isinstance(order_id, str)
            or not order_id
            or len(order_id) > 64
            or not isinstance(approval_url, str)
            or parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != _approval_host(self._environment)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PayPalProviderError("paypal_order_invalid_response")
        return PayPalOrderResult(order_id=order_id, approval_url=approval_url)

    def capture_order(self, *, order_id: str, idempotency_key: str) -> PayPalCaptureResult:
        token = self._token()
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=httpx.Timeout(10.0, connect=3.0),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    f"/v2/checkout/orders/{order_id}/capture",
                    json={},
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                        "PayPal-Request-Id": idempotency_key,
                        "Prefer": "return=representation",
                    },
                )
                if response.status_code >= 500:
                    raise PayPalProviderError("paypal_capture_ambiguous")
                if response.status_code >= 400:
                    raise PayPalProviderError("paypal_capture_rejected")
                data = response.json()
        except PayPalProviderError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise PayPalProviderError("paypal_capture_ambiguous") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise PayPalProviderError("paypal_capture_unavailable") from None

        status = data.get("status") if isinstance(data, dict) else None
        capture_id = None
        purchase_units = data.get("purchase_units") if isinstance(data, dict) else None
        if isinstance(purchase_units, list):
            for unit in purchase_units:
                payments = unit.get("payments", {}) if isinstance(unit, dict) else {}
                captures = payments.get("captures", []) if isinstance(payments, dict) else []
                if captures and isinstance(captures[0], dict):
                    capture_id = captures[0].get("id")
                    break
        if status not in {"COMPLETED", "PENDING", "APPROVED"}:
            raise PayPalProviderError("paypal_capture_invalid_response")
        return PayPalCaptureResult(order_id=order_id, status=status, capture_id=capture_id if isinstance(capture_id, str) else None)


def get_paypal_provider(*, settings=None, transport: httpx.BaseTransport | None = None) -> PayPalOrdersAdapter:
    try:
        settings = settings or load_billing_settings()
        paypal = settings.paypal
        if not settings.enabled or not settings.webhook_processing_enabled or not paypal.enabled:
            raise BillingConfigError("paypal_disabled")
        if paypal.environment == "production" and (not settings.live_charging_enabled or not paypal.production_approved):
            raise BillingConfigError("paypal_production_not_approved")
        if paypal.environment not in {"sandbox", "production"}:
            raise BillingConfigError("paypal_environment_invalid")
        client_secret = _read_secret_file("KLYROW_PAYPAL_SECRET_FILE", None)
        if not paypal.client_id or not paypal.api_base_url:
            raise BillingConfigError("paypal_credentials_missing")
    except BillingConfigError as exc:
        raise PayPalProviderError("paypal_disabled") from exc
    return PayPalOrdersAdapter(
        client_id=paypal.client_id,
        client_secret=client_secret,
        api_base_url=paypal.api_base_url,
        environment=paypal.environment,
        transport=transport,
    )


def verify_paypal_signature(
    body: bytes,
    headers: dict[str, str],
    *,
    webhook_id: str,
    environment: str,
    transport: httpx.BaseTransport | None = None,
    now_utc: datetime | None = None,
    tolerance_seconds: int = 300,
) -> None:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    transmission_id = normalized.get("paypal-transmission-id", "")
    transmission_time = normalized.get("paypal-transmission-time", "")
    cert_url = normalized.get("paypal-cert-url", "")
    auth_algo = normalized.get("paypal-auth-algo", "")
    transmission_sig = normalized.get("paypal-transmission-sig", "")
    if not all((transmission_id, transmission_time, cert_url, auth_algo, transmission_sig, webhook_id)):
        raise PayPalProviderError("paypal_webhook_headers_missing")
    if auth_algo.upper() != "SHA256WITHRSA":
        raise PayPalProviderError("paypal_webhook_algorithm_invalid")

    try:
        event_time = datetime.fromisoformat(transmission_time.replace("Z", "+00:00"))
        if event_time.tzinfo is None:
            raise ValueError
        current = now_utc or datetime.now(timezone.utc)
        if abs((current - event_time.astimezone(timezone.utc)).total_seconds()) > tolerance_seconds:
            raise PayPalProviderError("paypal_webhook_signature_expired")
    except PayPalProviderError:
        raise
    except (ValueError, TypeError):
        raise PayPalProviderError("paypal_webhook_timestamp_invalid") from None

    parsed = urlsplit(cert_url)
    if (
        environment not in {"sandbox", "production"}
        or parsed.scheme != "https"
        or parsed.hostname != _cert_host(environment)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path.startswith("/v1/notifications/certs/")
        or parsed.query
        or parsed.fragment
    ):
        raise PayPalProviderError("paypal_webhook_cert_url_invalid")

    try:
        with httpx.Client(transport=transport, timeout=httpx.Timeout(5.0, connect=2.0), trust_env=False, follow_redirects=False) as client:
            response = client.get(cert_url, headers={"Accept": "application/x-pem-file, text/plain"})
            response.raise_for_status()
            if len(response.content) > 65536:
                raise PayPalProviderError("paypal_webhook_cert_invalid")
            cert = x509.load_pem_x509_certificate(response.content)
    except PayPalProviderError:
        raise
    except (httpx.HTTPError, ValueError):
        raise PayPalProviderError("paypal_webhook_cert_unavailable") from None

    current = now_utc or datetime.now(timezone.utc)
    if not (cert.not_valid_before_utc <= current <= cert.not_valid_after_utc):
        raise PayPalProviderError("paypal_webhook_cert_expired")
    key = cert.public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        raise PayPalProviderError("paypal_webhook_cert_invalid")

    crc = zlib.crc32(body) & 0xFFFFFFFF
    signed = f"{transmission_id}|{transmission_time}|{webhook_id}|{crc}".encode("utf-8")
    try:
        signature = base64.b64decode(transmission_sig, validate=True)
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, InvalidSignature):
        raise PayPalProviderError("paypal_webhook_signature_invalid") from None


def safe_paypal_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
