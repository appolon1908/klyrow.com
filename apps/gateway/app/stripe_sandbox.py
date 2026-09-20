"""Sandbox-only Stripe hosted Checkout boundary.

This module never receives card data. Callers persist their local attempt and
idempotency key before invoking it; ambiguous provider responses are surfaced
without changing local payment state.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .billing_config import BillingConfigError, _read_secret_file, load_billing_settings


STRIPE_SANDBOX_API = "https://api.stripe.com/v1"
STRIPE_CHECKOUT_HOSTS = frozenset({"checkout.stripe.com"})


def get_checkout_provider(provider: str, *, settings=None) -> "StripeSandboxAdapter":
    if provider != "stripe":
        raise StripeWebhookError("unsupported_checkout_provider")
    try:
        settings = settings or load_billing_settings()
        if not settings.enabled or not settings.webhook_processing_enabled:
            raise BillingConfigError("billing_checkout_disabled")
        if not settings.stripe.enabled or settings.stripe.environment != "sandbox":
            raise BillingConfigError("stripe_sandbox_required")
        secret = _read_secret_file("KLYROW_STRIPE_SECRET_FILE", None)
    except BillingConfigError as exc:
        raise StripeWebhookError("billing_checkout_disabled") from exc
    return StripeSandboxAdapter(secret)


class StripeWebhookError(RuntimeError):
    pass


@dataclass(frozen=True)
class StripeCheckoutResult:
    session_id: str
    checkout_url: str


def verify_stripe_signature(body: bytes, signature: str, secret: str, *, now_epoch: Optional[int] = None, tolerance_seconds: int = 300) -> None:
    """Verify the Stripe-Signature header against the exact received bytes."""
    values: dict[str, list[str]] = {}
    for item in signature.split(","):
        key, separator, value = item.partition("=")
        if separator and key and value:
            values.setdefault(key, []).append(value)
    try:
        timestamp = int(values["t"][0])
    except (KeyError, ValueError):
        raise StripeWebhookError("stripe_webhook_signature_invalid") from None
    current = int(time.time()) if now_epoch is None else now_epoch
    if abs(current - timestamp) > tolerance_seconds:
        raise StripeWebhookError("stripe_webhook_signature_expired")
    expected = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(candidate, expected) for candidate in values.get("v1", ())):
        raise StripeWebhookError("stripe_webhook_signature_invalid")


class StripeSandboxAdapter:
    def __init__(self, secret: str, *, transport: httpx.BaseTransport | None = None) -> None:
        if not secret.startswith(("sk_test_", "rk_test_")):
            raise StripeWebhookError("stripe_sandbox_key_required")
        self._secret = secret
        self._transport = transport

    def create_checkout(self, *, payment_attempt_id: str, invoice_id: str, tenant_id: str, amount_minor: int, currency: str, idempotency_key: str, success_url: str, cancel_url: str) -> StripeCheckoutResult:
        if amount_minor <= 0 or not currency.isupper() or len(currency) != 3:
            raise StripeWebhookError("stripe_checkout_invalid_amount")
        payload = {
            "mode": "payment",
            "success_url": success_url + "?checkout_session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url,
            "client_reference_id": invoice_id,
            "metadata[invoice_id]": invoice_id,
            "metadata[tenant_id]": tenant_id,
            "metadata[payment_attempt_id]": payment_attempt_id,
            "payment_intent_data[metadata][invoice_id]": invoice_id,
            "payment_intent_data[metadata][tenant_id]": tenant_id,
            "payment_intent_data[metadata][payment_attempt_id]": payment_attempt_id,
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_minor),
            "line_items[0][price_data][product_data][name]": "Klyrow invoice " + invoice_id,
            "line_items[0][quantity]": "1",
        }
        try:
            with httpx.Client(base_url=STRIPE_SANDBOX_API, transport=self._transport, timeout=httpx.Timeout(10.0, connect=3.0), trust_env=False, follow_redirects=False) as client:
                response = client.post("/checkout/sessions", data=payload, headers={"Authorization": "Bearer " + self._secret, "Idempotency-Key": idempotency_key})
                response.raise_for_status()
                data = response.json()
        except (httpx.TimeoutException, httpx.TransportError):
            raise StripeWebhookError("stripe_checkout_ambiguous") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise StripeWebhookError("stripe_checkout_unavailable") from None
        session_id = data.get("id") if isinstance(data, dict) else None
        checkout_url = data.get("url") if isinstance(data, dict) else None
        parsed_url = urlsplit(checkout_url) if isinstance(checkout_url, str) else None
        if (
            not isinstance(session_id, str)
            or not session_id.startswith("cs_")
            or not isinstance(checkout_url, str)
            or parsed_url is None
            or parsed_url.scheme != "https"
            or parsed_url.hostname not in STRIPE_CHECKOUT_HOSTS
            or parsed_url.username is not None
            or parsed_url.password is not None
            or not parsed_url.path
        ):
            raise StripeWebhookError("stripe_checkout_invalid_response")
        return StripeCheckoutResult(session_id=session_id, checkout_url=checkout_url)