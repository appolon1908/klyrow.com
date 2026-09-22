"""Fail-closed billing configuration authority (Mission 01).

Validates all billing configuration before any database connection, worker
scheduling, provider client construction, billing mutation, or external
request. Every capability defaults to disabled. No provider SDK is used or
required by this module.

Secret material is resolved only through bounded, regular-file-only readers
and is never stored beyond the boolean "is this configured" fact needed by
:class:`StripeSettings`/:class:`PayPalSettings`/:class:`StablecoinSettings`.
Nothing here ever returns, logs, or chains a secret path or secret value.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MAX_SECRET_BYTES = 65536
_ENVIRONMENTS = frozenset({"sandbox", "production"})
_PROVIDERS = ("stripe", "paypal", "stablecoin")
_CURRENCY = __import__("re").compile(r"^[A-Z]{3}$")

FLAG_NAMES = {
    "billing_enabled": "KLYROW_BILLING_ENABLED",
    "live_charging_enabled": "KLYROW_LIVE_CHARGING_ENABLED",
    "stripe_enabled": "KLYROW_STRIPE_ENABLED",
    "paypal_enabled": "KLYROW_PAYPAL_ENABLED",
    "stablecoin_enabled": "KLYROW_STABLECOIN_ENABLED",
    "webhook_processing_enabled": "KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED",
    "dunning_enabled": "KLYROW_BILLING_DUNNING_ENABLED",
    "refunds_enabled": "KLYROW_BILLING_REFUNDS_ENABLED",
    "dispute_actions_enabled": "KLYROW_BILLING_DISPUTE_ACTIONS_ENABLED",
    "reconciliation_enabled": "KLYROW_BILLING_RECONCILIATION_ENABLED",
}

# Known real-world shape markers used only to reject an obviously mismatched
# key/endpoint; never used to validate that a key is genuine.
_PRODUCTION_KEY_PREFIXES = {"stripe": ("sk_live_", "rk_live_")}
_SANDBOX_KEY_PREFIXES = {"stripe": ("sk_test_", "rk_test_")}
_PRODUCTION_ENDPOINT_MARKERS = {"paypal": ("api-m.paypal.com",)}
_SANDBOX_ENDPOINT_MARKERS = {"paypal": ("api-m.sandbox.paypal.com",)}


class BillingConfigError(ValueError):
    """Raised with a stable, safe error code; never carries a secret or path."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


def _boolean(name: str, environment: Mapping[str, str] | None) -> bool:
    source = os.environ if environment is None else environment
    if name not in source:
        return False
    raw = source[name]
    value = raw.strip().lower() if isinstance(raw, str) else ""
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise BillingConfigError("invalid_boolean", f"{name} must be a documented boolean form")


def _text(name: str, environment: Mapping[str, str] | None, default: str = "") -> str:
    source = os.environ if environment is None else environment
    value = source.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _csv(name: str, environment: Mapping[str, str] | None) -> tuple[str, ...]:
    raw = _text(name, environment)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _read_secret_file(var_name: str, environment: Mapping[str, str] | None) -> str:
    """Read and return a secret's content; never chain the underlying OSError.

    Fails closed on: missing reference, missing file, symlink, non-regular
    file, world-writable file, oversized content, and non-UTF-8 content.
    """

    source = os.environ if environment is None else environment
    raw = source.get(var_name, "")
    path_value = raw.strip() if isinstance(raw, str) else ""
    if not path_value:
        raise BillingConfigError("secret_reference_missing", f"{var_name} is required")
    try:
        info = os.lstat(path_value)
    except OSError:
        raise BillingConfigError("secret_file_unavailable", f"{var_name} is unavailable") from None
    if stat.S_ISLNK(info.st_mode):
        raise BillingConfigError("secret_file_symlink_rejected", f"{var_name} must not be a symlink") from None
    if not stat.S_ISREG(info.st_mode):
        raise BillingConfigError("secret_file_not_regular", f"{var_name} must be a regular file") from None
    if os.name == "posix" and info.st_mode & stat.S_IWOTH:
        raise BillingConfigError("secret_file_world_writable", f"{var_name} must not be world-writable") from None
    try:
        with open(path_value, "rb") as handle:
            content = handle.read(_MAX_SECRET_BYTES + 1)
    except OSError:
        raise BillingConfigError("secret_file_unavailable", f"{var_name} is unavailable") from None
    if len(content) > _MAX_SECRET_BYTES:
        raise BillingConfigError("secret_file_oversized", f"{var_name} is oversized") from None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise BillingConfigError("secret_file_invalid_encoding", f"{var_name} must be UTF-8 encoded") from None
    value = text.strip()
    if not value:
        raise BillingConfigError("secret_file_empty", f"{var_name} must be a non-empty secret file") from None
    return value


@dataclass(frozen=True)
class ProviderSettings:
    enabled: bool = False
    environment: str = "sandbox"
    production_approved: bool = False
    secret_configured: bool = False
    webhook_secret_configured: bool = False
    currency_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class StripeSettings(ProviderSettings):
    pass


@dataclass(frozen=True)
class PayPalSettings(ProviderSettings):
    client_id: str | None = field(default=None, repr=False)
    api_base_url: str = ""


@dataclass(frozen=True)
class StablecoinSettings(ProviderSettings):
    chain_id: int | None = None
    usdc_contract: str | None = field(default=None, repr=False)
    decimals: int | None = None
    confirmation_threshold: int | None = None
    network_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class BillingCapabilityStatus:
    enabled: bool
    live_charging_enabled: bool
    providers: dict[str, str]
    blocked_reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "billing": {
                "enabled": self.enabled,
                "live_charging": self.live_charging_enabled,
                "providers": dict(self.providers),
                "blocked_reasons": list(self.blocked_reasons),
            }
        }


@dataclass(frozen=True)
class BillingSettings:
    enabled: bool
    live_charging_enabled: bool
    webhook_processing_enabled: bool
    dunning_enabled: bool
    refunds_enabled: bool
    dispute_actions_enabled: bool
    reconciliation_enabled: bool
    stripe: StripeSettings
    paypal: PayPalSettings
    stablecoin: StablecoinSettings

    @property
    def active_providers(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, settings in (("stripe", self.stripe), ("paypal", self.paypal), ("stablecoin", self.stablecoin))
            if settings.enabled
        )

    def capability_status(self) -> BillingCapabilityStatus:
        reasons: list[str] = []
        if not self.enabled:
            reasons.append("billing_disabled")
        providers: dict[str, str] = {}
        for name, settings in (("stripe", self.stripe), ("paypal", self.paypal), ("stablecoin", self.stablecoin)):
            if not settings.enabled:
                providers[name] = "disabled"
            else:
                providers[name] = settings.environment
        if self.enabled and not self.active_providers:
            reasons.append("no_provider_enabled")
        if self.live_charging_enabled and not self.active_providers:
            reasons.append("live_charging_requires_provider")
        if not reasons:
            reasons.append("ok")
        return BillingCapabilityStatus(
            enabled=self.enabled,
            live_charging_enabled=self.live_charging_enabled,
            providers=providers,
            blocked_reasons=tuple(reasons),
        )


_DISABLED_STRIPE = StripeSettings()
_DISABLED_PAYPAL = PayPalSettings()
_DISABLED_STABLECOIN = StablecoinSettings()
_DISABLED_SETTINGS = BillingSettings(
    enabled=False,
    live_charging_enabled=False,
    webhook_processing_enabled=False,
    dunning_enabled=False,
    refunds_enabled=False,
    dispute_actions_enabled=False,
    reconciliation_enabled=False,
    stripe=_DISABLED_STRIPE,
    paypal=_DISABLED_PAYPAL,
    stablecoin=_DISABLED_STABLECOIN,
)


def _environment_mode(provider: str, environment: Mapping[str, str] | None) -> str:
    name = f"KLYROW_{provider.upper()}_ENVIRONMENT"
    value = _text(name, environment, "sandbox").lower()
    if value not in _ENVIRONMENTS:
        raise BillingConfigError("provider_environment_invalid", f"{name} must be sandbox or production")
    return value


def _reject_key_shape_mismatch(provider: str, mode: str, secret_value: str) -> None:
    if mode == "sandbox":
        for prefix in _PRODUCTION_KEY_PREFIXES.get(provider, ()):
            if secret_value.startswith(prefix):
                raise BillingConfigError("sandbox_production_key_rejected", f"{provider} sandbox must not use a production key")
    else:
        for prefix in _SANDBOX_KEY_PREFIXES.get(provider, ()):
            if secret_value.startswith(prefix):
                raise BillingConfigError("production_sandbox_key_rejected", f"{provider} production must not use a sandbox key")


def _reject_endpoint_mismatch(provider: str, mode: str, environment: Mapping[str, str] | None) -> None:
    endpoint = _text(f"KLYROW_{provider.upper()}_API_BASE_URL", environment).lower()
    if not endpoint:
        return
    if mode == "sandbox":
        for marker in _PRODUCTION_ENDPOINT_MARKERS.get(provider, ()):
            if marker in endpoint:
                raise BillingConfigError("sandbox_production_endpoint_rejected", f"{provider} sandbox must not target a production endpoint")
    else:
        for marker in _SANDBOX_ENDPOINT_MARKERS.get(provider, ()):
            if marker in endpoint:
                raise BillingConfigError("production_sandbox_endpoint_rejected", f"{provider} production must not target a sandbox endpoint")


def _reject_inline_secret(provider: str, mode: str, environment: Mapping[str, str] | None) -> None:
    source = os.environ if environment is None else environment
    if mode == "production" and _text(f"KLYROW_{provider.upper()}_SECRET", environment):
        raise BillingConfigError("production_inline_secret_rejected", f"{provider} production must not use an inline secret")


def _validate_currency_allowlist(provider: str, mode: str, environment: Mapping[str, str] | None) -> tuple[str, ...]:
    values = _csv(f"KLYROW_{provider.upper()}_CURRENCY_ALLOWLIST", environment)
    if mode == "production":
        if not values:
            raise BillingConfigError("production_missing_currency_allowlist", f"{provider} production requires a currency allowlist")
        if any(not _CURRENCY.fullmatch(value) for value in values):
            raise BillingConfigError("currency_allowlist_invalid", f"{provider} currency allowlist must use ISO 4217 codes")
    return values


def _validate_provider_common(
    provider: str,
    environment: Mapping[str, str] | None,
    webhook_processing_enabled: bool,
    settings_cls: type[ProviderSettings],
) -> ProviderSettings:
    mode = _environment_mode(provider, environment)
    _reject_inline_secret(provider, mode, environment)
    secret_value = _read_secret_file(f"KLYROW_{provider.upper()}_SECRET_FILE", environment)
    _reject_key_shape_mismatch(provider, mode, secret_value)
    _reject_endpoint_mismatch(provider, mode, environment)
    currency_allowlist = _validate_currency_allowlist(provider, mode, environment)

    webhook_secret_configured = False
    webhook_required = webhook_processing_enabled or mode == "production"
    if webhook_required:
        _read_secret_file(f"KLYROW_{provider.upper()}_WEBHOOK_SECRET_FILE", environment)
        webhook_secret_configured = True

    production_approved = _boolean(f"KLYROW_{provider.upper()}_PRODUCTION_APPROVED", environment)
    if mode == "production" and not production_approved:
        raise BillingConfigError("production_requires_approval", f"{provider} production requires explicit approval")

    return settings_cls(
        enabled=True,
        environment=mode,
        production_approved=production_approved,
        secret_configured=True,
        webhook_secret_configured=webhook_secret_configured,
        currency_allowlist=currency_allowlist,
    )


def _validate_paypal(environment: Mapping[str, str] | None, webhook_processing_enabled: bool) -> PayPalSettings:
    common = _validate_provider_common("paypal", environment, webhook_processing_enabled, PayPalSettings)
    client_id = _text("KLYROW_PAYPAL_CLIENT_ID", environment)
    if not client_id:
        raise BillingConfigError("paypal_client_id_missing", "paypal requires KLYROW_PAYPAL_CLIENT_ID")

    expected_host = "api-m.sandbox.paypal.com" if common.environment == "sandbox" else "api-m.paypal.com"
    configured = _text("KLYROW_PAYPAL_API_BASE_URL", environment)
    api_base_url = configured or f"https://{expected_host}"
    parsed = urlsplit(api_base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise BillingConfigError("paypal_api_base_url_invalid", "paypal API base URL must match the selected environment")

    return PayPalSettings(
        enabled=True,
        environment=common.environment,
        production_approved=common.production_approved,
        secret_configured=common.secret_configured,
        webhook_secret_configured=common.webhook_secret_configured,
        currency_allowlist=common.currency_allowlist,
        client_id=client_id,
        api_base_url=api_base_url.rstrip("/"),
    )


def _validate_stablecoin(environment: Mapping[str, str] | None, webhook_processing_enabled: bool) -> StablecoinSettings:
    common = _validate_provider_common("stablecoin", environment, webhook_processing_enabled, StablecoinSettings)

    chain_id_raw = _text("KLYROW_STABLECOIN_CHAIN_ID", environment)
    if not chain_id_raw:
        raise BillingConfigError("stablecoin_missing_chain_id", "stablecoin requires an explicit chain ID")
    try:
        chain_id = int(chain_id_raw)
    except ValueError:
        raise BillingConfigError("stablecoin_missing_chain_id", "stablecoin chain ID must be an integer") from None

    contract = _text("KLYROW_STABLECOIN_USDC_CONTRACT", environment)
    if not contract:
        raise BillingConfigError("stablecoin_missing_contract", "stablecoin requires a USDC contract/mint reference")

    decimals_raw = _text("KLYROW_STABLECOIN_DECIMALS", environment)
    if not decimals_raw:
        raise BillingConfigError("stablecoin_invalid_decimals", "stablecoin requires explicit decimals")
    try:
        decimals = int(decimals_raw)
    except ValueError:
        decimals = -1
    if not 0 <= decimals <= 18:
        raise BillingConfigError("stablecoin_invalid_decimals", "stablecoin decimals must be between 0 and 18")

    threshold_raw = _text("KLYROW_STABLECOIN_CONFIRMATION_THRESHOLD", environment)
    if not threshold_raw:
        raise BillingConfigError("stablecoin_missing_confirmation_threshold", "stablecoin requires a confirmation threshold")
    try:
        threshold = int(threshold_raw)
    except ValueError:
        threshold = 0
    if threshold < 1:
        raise BillingConfigError("stablecoin_missing_confirmation_threshold", "stablecoin confirmation threshold must be at least 1")

    network_allowlist = _csv("KLYROW_STABLECOIN_NETWORK_ALLOWLIST", environment)
    if not network_allowlist:
        raise BillingConfigError("stablecoin_missing_network_allowlist", "stablecoin requires an approved network allowlist")
    if str(chain_id) not in network_allowlist:
        raise BillingConfigError("stablecoin_chain_not_allowlisted", "stablecoin chain ID is not in the approved network allowlist")

    return StablecoinSettings(
        enabled=True,
        environment=common.environment,
        production_approved=common.production_approved,
        secret_configured=common.secret_configured,
        webhook_secret_configured=common.webhook_secret_configured,
        currency_allowlist=common.currency_allowlist,
        chain_id=chain_id,
        usdc_contract=contract,
        decimals=decimals,
        confirmation_threshold=threshold,
        network_allowlist=network_allowlist,
    )


def load_billing_settings(environment: Mapping[str, str] | None = None) -> BillingSettings:
    """Parse and fail-closed validate all billing configuration.

    Safe to call before any database engine, worker task, or provider client
    exists: performs no network access, no database access, and no billing
    mutation. Every dependent flag requires its prerequisite; invalid or
    unsupported combinations raise :class:`BillingConfigError`.
    """

    billing_enabled = _boolean(FLAG_NAMES["billing_enabled"], environment)
    dependents = {
        key: _boolean(FLAG_NAMES[key], environment)
        for key in (
            "live_charging_enabled",
            "stripe_enabled",
            "paypal_enabled",
            "stablecoin_enabled",
            "webhook_processing_enabled",
            "dunning_enabled",
            "refunds_enabled",
            "dispute_actions_enabled",
            "reconciliation_enabled",
        )
    }

    if not billing_enabled:
        enabled_dependents = [key for key, value in dependents.items() if value]
        if enabled_dependents:
            names = ", ".join(FLAG_NAMES[key] for key in enabled_dependents)
            raise BillingConfigError("billing_disabled_dependency", f"KLYROW_BILLING_ENABLED must be enabled before: {names}")
        return _DISABLED_SETTINGS

    webhook_processing_enabled = dependents["webhook_processing_enabled"]

    stripe = (
        _validate_provider_common("stripe", environment, webhook_processing_enabled, StripeSettings)
        if dependents["stripe_enabled"]
        else _DISABLED_STRIPE
    )

    paypal = (
        _validate_paypal(environment, webhook_processing_enabled)
        if dependents["paypal_enabled"]
        else _DISABLED_PAYPAL
    )

    stablecoin = (
        _validate_stablecoin(environment, webhook_processing_enabled)
        if dependents["stablecoin_enabled"]
        else _DISABLED_STABLECOIN
    )

    active_providers = [
        name
        for name, active in (("stripe", dependents["stripe_enabled"]), ("paypal", dependents["paypal_enabled"]), ("stablecoin", dependents["stablecoin_enabled"]))
        if active
    ]

    if dependents["live_charging_enabled"] and not active_providers:
        raise BillingConfigError("live_charging_requires_provider", "KLYROW_LIVE_CHARGING_ENABLED requires at least one enabled provider")

    if webhook_processing_enabled and not active_providers:
        raise BillingConfigError("webhook_processing_requires_provider", "KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED requires at least one configured provider verifier")

    return BillingSettings(
        enabled=True,
        live_charging_enabled=dependents["live_charging_enabled"],
        webhook_processing_enabled=webhook_processing_enabled,
        dunning_enabled=dependents["dunning_enabled"],
        refunds_enabled=dependents["refunds_enabled"],
        dispute_actions_enabled=dependents["dispute_actions_enabled"],
        reconciliation_enabled=dependents["reconciliation_enabled"],
        stripe=stripe,
        paypal=paypal,
        stablecoin=stablecoin,
    )


def billing_capability_status_payload() -> dict:
    """Safe, allowlisted status payload for an authenticated capability-status route.

    Never returns a path, secret, provider account ID, or exception string;
    invalid configuration is reported the same as fully-disabled, since the
    route only advertises capability shape, not raw validation errors.
    """

    try:
        settings = load_billing_settings()
    except BillingConfigError:
        settings = _DISABLED_SETTINGS
    return settings.capability_status().as_dict()
