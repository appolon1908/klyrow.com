"""Phase 0 fail-closed billing activation configuration.

This module only parses environment flags and validates secret *references*
(file paths). It never contacts a payment provider, never charges, refunds,
transmits, or mutates billing data, and never logs or returns secret file
content. Importing this module has no side effects; validation only runs when
``validate_billing_activation`` is called explicitly (for example, at gateway
startup).

PaymentAttempt, provider adapters, ledger/journal entries, durable webhook
ingestion, dunning execution, and live charging are out of scope for Phase 0
and are not implemented here. See docs/architecture/billing-target-architecture.md.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MAX_SECRET_BYTES = 65536

PROVIDERS = ("stripe", "paypal", "stablecoin")

FLAG_NAMES = {
    "core": "KLYROW_BILLING_CORE_ENABLED",
    "entitlements": "KLYROW_BILLING_ENTITLEMENTS_ENABLED",
    "provider_webhooks": "KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED",
    "stripe": "KLYROW_BILLING_STRIPE_ENABLED",
    "paypal": "KLYROW_BILLING_PAYPAL_ENABLED",
    "stablecoin": "KLYROW_BILLING_STABLECOIN_ENABLED",
    "dunning": "KLYROW_BILLING_DUNNING_ENABLED",
    "live_charging": "KLYROW_BILLING_LIVE_CHARGING_ENABLED",
}

SECRET_REFERENCE_NAMES = {
    "stripe": "KLYROW_BILLING_STRIPE_SECRET_FILE",
    "stripe_webhook": "KLYROW_BILLING_STRIPE_WEBHOOK_SECRET_FILE",
    "paypal": "KLYROW_BILLING_PAYPAL_SECRET_FILE",
    "paypal_webhook": "KLYROW_BILLING_PAYPAL_WEBHOOK_SECRET_FILE",
    "stablecoin": "KLYROW_BILLING_STABLECOIN_SECRET_FILE",
    "stablecoin_webhook": "KLYROW_BILLING_STABLECOIN_WEBHOOK_SECRET_FILE",
}


class BillingActivationError(ValueError):
    """Raised when billing activation configuration is invalid or unsafe."""


@dataclass(frozen=True)
class BillingActivationState:
    core_enabled: bool
    entitlements_enabled: bool
    provider_webhooks_enabled: bool
    stripe_enabled: bool
    paypal_enabled: bool
    stablecoin_enabled: bool
    dunning_enabled: bool
    live_charging_enabled: bool

    @property
    def active_providers(self) -> tuple[str, ...]:
        return tuple(p for p in PROVIDERS if getattr(self, f"{p}_enabled"))


_DISABLED_STATE = BillingActivationState(
    core_enabled=False,
    entitlements_enabled=False,
    provider_webhooks_enabled=False,
    stripe_enabled=False,
    paypal_enabled=False,
    stablecoin_enabled=False,
    dunning_enabled=False,
    live_charging_enabled=False,
)


def _boolean(name: str, environment: Mapping[str, str] | None) -> bool:
    """Parse a documented boolean form. Missing means disabled; anything else fails closed."""

    source = os.environ if environment is None else environment
    if name not in source:
        return False
    value = str(source[name]).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise BillingActivationError(
        f"{name} must be one of the documented boolean forms (true/false/yes/no/on/off/1/0)"
    )


def _read_flags(environment: Mapping[str, str] | None) -> dict[str, bool]:
    return {key: _boolean(name, environment) for key, name in FLAG_NAMES.items()}


def _validate_secret_reference(var_name: str, environment: Mapping[str, str] | None) -> None:
    """Validate a secret-reference path without disclosing its path or content.

    Reads at most ``_MAX_SECRET_BYTES + 1`` bytes (never the whole file) and never
    chains the underlying OSError/UnicodeDecodeError, so neither the raised
    exception nor its formatted traceback contains the secret path or content.
    """

    source = os.environ if environment is None else environment
    raw = source.get(var_name, "")
    path_value = raw.strip() if isinstance(raw, str) else ""
    if not path_value:
        raise BillingActivationError(f"{var_name} is required and must reference a secret file")
    try:
        with open(path_value, "rb") as handle:
            content = handle.read(_MAX_SECRET_BYTES + 1)
    except OSError:
        raise BillingActivationError(f"{var_name} must reference a readable secret file") from None
    if len(content) > _MAX_SECRET_BYTES:
        raise BillingActivationError(f"{var_name} secret file is oversized") from None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise BillingActivationError(f"{var_name} must reference a UTF-8 encoded secret file") from None
    if not text.strip():
        raise BillingActivationError(f"{var_name} must reference a non-empty secret file")


def validate_billing_activation(
    environment: Mapping[str, str] | None = None,
) -> BillingActivationState:
    """Parse and fail-closed validate billing activation configuration.

    Safe to call at gateway startup: never contacts a payment provider, and
    never charges, refunds, transmits, or mutates billing data.
    """

    flags = _read_flags(environment)

    if not flags["core"]:
        dependents = [
            key
            for key in ("entitlements", "provider_webhooks", *PROVIDERS, "dunning", "live_charging")
            if flags[key]
        ]
        if dependents:
            names = ", ".join(FLAG_NAMES[key] for key in dependents)
            raise BillingActivationError(
                f"{FLAG_NAMES['core']} must be enabled before activating: {names}"
            )
        return _DISABLED_STATE

    for provider in PROVIDERS:
        if flags[provider]:
            _validate_secret_reference(SECRET_REFERENCE_NAMES[provider], environment)

    active_providers = [p for p in PROVIDERS if flags[p]]

    if flags["provider_webhooks"]:
        if not active_providers:
            raise BillingActivationError(
                f"{FLAG_NAMES['provider_webhooks']} requires at least one enabled payment provider"
            )
        for provider in active_providers:
            _validate_secret_reference(SECRET_REFERENCE_NAMES[f"{provider}_webhook"], environment)

    if flags["dunning"] and not flags["entitlements"]:
        raise BillingActivationError(
            f"{FLAG_NAMES['dunning']} requires {FLAG_NAMES['entitlements']}"
        )

    if flags["live_charging"] and not active_providers:
        raise BillingActivationError(
            f"{FLAG_NAMES['live_charging']} requires at least one enabled and valid payment provider"
        )

    return BillingActivationState(
        core_enabled=True,
        entitlements_enabled=flags["entitlements"],
        provider_webhooks_enabled=flags["provider_webhooks"],
        stripe_enabled=flags["stripe"],
        paypal_enabled=flags["paypal"],
        stablecoin_enabled=flags["stablecoin"],
        dunning_enabled=flags["dunning"],
        live_charging_enabled=flags["live_charging"],
    )


def billing_core_enabled(environment: Mapping[str, str] | None = None) -> bool:
    return validate_billing_activation(environment).core_enabled
