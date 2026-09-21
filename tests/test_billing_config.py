"""Mission 01: fail-closed billing configuration authority tests."""
import os

import pytest

from apps.gateway.app.billing_config import (
    FLAG_NAMES,
    BillingConfigError,
    load_billing_settings,
)

ALL_FLAGS = list(FLAG_NAMES.values())
SYNTHETIC_SECRET = "SYNTHETIC-FIXTURE-VALUE-NOT-A-CREDENTIAL-0000111122223333"


def env(**overrides):
    base = {name: "false" for name in ALL_FLAGS}
    base.update(overrides)
    return base


def write_secret(tmp_path, name, content=SYNTHETIC_SECRET):
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def enabled_stripe_env(tmp_path, **overrides):
    values = env(
        KLYROW_BILLING_ENABLED="true",
        KLYROW_STRIPE_ENABLED="true",
        KLYROW_STRIPE_SECRET_FILE=write_secret(tmp_path, "stripe-secret"),
    )
    values.update(overrides)
    return values


# ---- defaults ----


def test_all_defaults_are_disabled():
    settings = load_billing_settings({})
    assert settings.enabled is False
    assert settings.live_charging_enabled is False
    assert settings.webhook_processing_enabled is False
    assert settings.dunning_enabled is False
    assert settings.refunds_enabled is False
    assert settings.dispute_actions_enabled is False
    assert settings.reconciliation_enabled is False
    assert settings.stripe.enabled is False
    assert settings.paypal.enabled is False
    assert settings.stablecoin.enabled is False


@pytest.mark.parametrize("value", ["", "enabled", "2", "TRUEISH", " "])
def test_malformed_booleans_fail(value):
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(env(KLYROW_BILLING_ENABLED=value))
    assert excinfo.value.code == "invalid_boolean"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_documented_true_forms_accepted(value):
    assert load_billing_settings(env(KLYROW_BILLING_ENABLED=value)).enabled is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_documented_false_forms_accepted(value):
    assert load_billing_settings(env(KLYROW_BILLING_ENABLED=value)).enabled is False


# ---- core dependency rules ----


@pytest.mark.parametrize(
    "flag",
    [
        "KLYROW_LIVE_CHARGING_ENABLED",
        "KLYROW_STRIPE_ENABLED",
        "KLYROW_PAYPAL_ENABLED",
        "KLYROW_STABLECOIN_ENABLED",
        "KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED",
        "KLYROW_BILLING_DUNNING_ENABLED",
        "KLYROW_BILLING_REFUNDS_ENABLED",
        "KLYROW_BILLING_DISPUTE_ACTIONS_ENABLED",
        "KLYROW_BILLING_RECONCILIATION_ENABLED",
    ],
)
def test_provider_and_dependent_flags_fail_without_billing(flag):
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(env(**{flag: "true"}))
    assert excinfo.value.code == "billing_disabled_dependency"


def test_live_charging_fails_without_any_provider():
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(env(KLYROW_BILLING_ENABLED="true", KLYROW_LIVE_CHARGING_ENABLED="true"))
    assert excinfo.value.code == "live_charging_requires_provider"


def test_live_charging_succeeds_with_a_valid_provider(tmp_path):
    settings = load_billing_settings(enabled_stripe_env(tmp_path, KLYROW_LIVE_CHARGING_ENABLED="true"))
    assert settings.live_charging_enabled is True


def test_webhook_processing_fails_without_any_provider():
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(env(KLYROW_BILLING_ENABLED="true", KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED="true"))
    assert excinfo.value.code == "webhook_processing_requires_provider"


def test_webhook_processing_requires_provider_webhook_secret(tmp_path):
    with pytest.raises(BillingConfigError, match="KLYROW_STRIPE_WEBHOOK_SECRET_FILE"):
        load_billing_settings(enabled_stripe_env(tmp_path, KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED="true"))


def test_webhook_processing_succeeds_with_webhook_secret(tmp_path):
    values = enabled_stripe_env(
        tmp_path,
        KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED="true",
        KLYROW_STRIPE_WEBHOOK_SECRET_FILE=write_secret(tmp_path, "stripe-webhook-secret"),
    )
    settings = load_billing_settings(values)
    assert settings.webhook_processing_enabled is True
    assert settings.stripe.webhook_secret_configured is True


@pytest.mark.parametrize(
    "flag",
    [
        "KLYROW_BILLING_DUNNING_ENABLED",
        "KLYROW_BILLING_REFUNDS_ENABLED",
        "KLYROW_BILLING_DISPUTE_ACTIONS_ENABLED",
        "KLYROW_BILLING_RECONCILIATION_ENABLED",
    ],
)
def test_dependent_capabilities_activate_with_billing_enabled_only(flag):
    settings = load_billing_settings(env(KLYROW_BILLING_ENABLED="true", **{flag: "true"}))
    assert settings.enabled is True


def test_provider_without_billing_fails():
    with pytest.raises(BillingConfigError, match="KLYROW_BILLING_ENABLED"):
        load_billing_settings(env(KLYROW_STRIPE_ENABLED="true"))


# ---- production/sandbox rules ----


def test_production_requires_explicit_approval(tmp_path):
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(
            enabled_stripe_env(
                tmp_path,
                KLYROW_STRIPE_ENVIRONMENT="production",
                KLYROW_STRIPE_WEBHOOK_SECRET_FILE=write_secret(tmp_path, "stripe-webhook-secret"),
                KLYROW_STRIPE_CURRENCY_ALLOWLIST="USD",
            )
        )
    assert excinfo.value.code == "production_requires_approval"


def test_production_requires_webhook_secret_even_without_umbrella_flag(tmp_path):
    with pytest.raises(BillingConfigError, match="KLYROW_STRIPE_WEBHOOK_SECRET_FILE"):
        load_billing_settings(
            enabled_stripe_env(
                tmp_path,
                KLYROW_STRIPE_ENVIRONMENT="production",
                KLYROW_STRIPE_PRODUCTION_APPROVED="true",
                KLYROW_STRIPE_CURRENCY_ALLOWLIST="USD",
            )
        )


def test_production_requires_currency_allowlist(tmp_path):
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(
            enabled_stripe_env(
                tmp_path,
                KLYROW_STRIPE_ENVIRONMENT="production",
                KLYROW_STRIPE_PRODUCTION_APPROVED="true",
                KLYROW_STRIPE_WEBHOOK_SECRET_FILE=write_secret(tmp_path, "stripe-webhook-secret"),
            )
        )
    assert excinfo.value.code == "production_missing_currency_allowlist"


def test_production_with_valid_configuration_activates(tmp_path):
    settings = load_billing_settings(
        enabled_stripe_env(
            tmp_path,
            KLYROW_STRIPE_ENVIRONMENT="production",
            KLYROW_STRIPE_PRODUCTION_APPROVED="true",
            KLYROW_STRIPE_WEBHOOK_SECRET_FILE=write_secret(tmp_path, "stripe-webhook-secret"),
            KLYROW_STRIPE_CURRENCY_ALLOWLIST="USD,EUR",
        )
    )
    assert settings.stripe.environment == "production"
    assert settings.stripe.currency_allowlist == ("USD", "EUR")


def test_production_with_inline_secret_fails(tmp_path):
    values = enabled_stripe_env(
        tmp_path,
        KLYROW_STRIPE_ENVIRONMENT="production",
        KLYROW_STRIPE_PRODUCTION_APPROVED="true",
        KLYROW_STRIPE_WEBHOOK_SECRET_FILE=write_secret(tmp_path, "stripe-webhook-secret"),
        KLYROW_STRIPE_CURRENCY_ALLOWLIST="USD",
    )
    values["KLYROW_STRIPE_SECRET"] = "inline-secret-value"
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "production_inline_secret_rejected"


def test_sandbox_with_production_key_fails(tmp_path):
    values = enabled_stripe_env(
        tmp_path,
        KLYROW_STRIPE_SECRET_FILE=write_secret(tmp_path, "stripe-secret-live", "sk_live_should_be_rejected"),
    )
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "sandbox_production_key_rejected"


def test_sandbox_with_production_endpoint_fails(tmp_path):
    values = env(
        KLYROW_BILLING_ENABLED="true",
        KLYROW_PAYPAL_ENABLED="true",
        KLYROW_PAYPAL_SECRET_FILE=write_secret(tmp_path, "paypal-secret"),
        KLYROW_PAYPAL_API_BASE_URL="https://api-m.paypal.com/v1",
    )
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "sandbox_production_endpoint_rejected"


def test_invalid_provider_environment_value_fails(tmp_path):
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_ENVIRONMENT="staging")
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "provider_environment_invalid"


# ---- stablecoin ----


def stablecoin_env(tmp_path, **overrides):
    values = env(
        KLYROW_BILLING_ENABLED="true",
        KLYROW_STABLECOIN_ENABLED="true",
        KLYROW_STABLECOIN_SECRET_FILE=write_secret(tmp_path, "stablecoin-secret"),
        KLYROW_STABLECOIN_CHAIN_ID="1",
        KLYROW_STABLECOIN_USDC_CONTRACT="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        KLYROW_STABLECOIN_DECIMALS="6",
        KLYROW_STABLECOIN_CONFIRMATION_THRESHOLD="12",
        KLYROW_STABLECOIN_NETWORK_ALLOWLIST="1,137",
        KLYROW_STABLECOIN_CURRENCY_ALLOWLIST="USD",
        KLYROW_STABLECOIN_API_BASE_URL="https://rpc.example.test",
        KLYROW_STABLECOIN_RECEIVE_ADDRESS="0x1111111111111111111111111111111111111111",
        KLYROW_STABLECOIN_WALLET_REFERENCE="openbao://billing/usdc-receiver",
    )
    values.update(overrides)
    return values


def test_stablecoin_missing_network_or_contract_or_finality_fails(tmp_path):
    for missing, expected_code in (
        ("KLYROW_STABLECOIN_CHAIN_ID", "stablecoin_missing_chain_id"),
        ("KLYROW_STABLECOIN_USDC_CONTRACT", "stablecoin_missing_contract"),
        ("KLYROW_STABLECOIN_DECIMALS", "stablecoin_invalid_decimals"),
        ("KLYROW_STABLECOIN_CONFIRMATION_THRESHOLD", "stablecoin_missing_confirmation_threshold"),
        ("KLYROW_STABLECOIN_NETWORK_ALLOWLIST", "stablecoin_missing_network_allowlist"),
    ):
        values = stablecoin_env(tmp_path)
        values[missing] = ""
        with pytest.raises(BillingConfigError) as excinfo:
            load_billing_settings(values)
        assert excinfo.value.code == expected_code, missing


def test_stablecoin_chain_not_in_allowlist_fails(tmp_path):
    values = stablecoin_env(tmp_path, KLYROW_STABLECOIN_NETWORK_ALLOWLIST="137")
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "stablecoin_chain_not_allowlisted"


def test_stablecoin_activates_with_full_configuration(tmp_path):
    settings = load_billing_settings(stablecoin_env(tmp_path))
    assert settings.stablecoin.enabled is True
    assert settings.stablecoin.chain_id == 1
    assert settings.stablecoin.confirmation_threshold == 12


# ---- secret-file safety matrix ----


def test_secret_reference_missing_fails(tmp_path):
    values = env(KLYROW_BILLING_ENABLED="true", KLYROW_STRIPE_ENABLED="true")
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_reference_missing"


def test_secret_file_missing_fails(tmp_path):
    values = enabled_stripe_env(tmp_path)
    values["KLYROW_STRIPE_SECRET_FILE"] = str(tmp_path / "does-not-exist")
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_file_unavailable"


def test_secret_file_symlink_fails(tmp_path):
    real = tmp_path / "real-secret"
    real.write_text(SYNTHETIC_SECRET)
    link = tmp_path / "linked-secret"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted in this environment")
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_SECRET_FILE=str(link))
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_file_symlink_rejected"


def test_secret_file_not_regular_fails(tmp_path):
    directory = tmp_path / "a-directory"
    directory.mkdir()
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_SECRET_FILE=str(directory))
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_file_not_regular"


def test_secret_file_world_writable_fails_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("world-writable bit is not meaningful on this platform")
    secret_file = tmp_path / "world-writable-secret"
    secret_file.write_text(SYNTHETIC_SECRET)
    secret_file.chmod(0o666)
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_SECRET_FILE=str(secret_file))
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_file_world_writable"


def test_secret_file_oversized_fails(tmp_path):
    secret_file = tmp_path / "oversized-secret"
    secret_file.write_bytes(b"a" * (65536 + 1))
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_SECRET_FILE=str(secret_file))
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_file_oversized"


def test_secret_file_invalid_utf8_fails(tmp_path):
    secret_file = tmp_path / "invalid-encoding-secret"
    secret_file.write_bytes(b"\xff\xfe\x00invalid")
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_SECRET_FILE=str(secret_file))
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_file_invalid_encoding"


def test_secret_file_empty_fails(tmp_path):
    secret_file = tmp_path / "empty-secret"
    secret_file.write_text("   ")
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_SECRET_FILE=str(secret_file))
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "secret_file_empty"


# ---- safety of error/log output ----


def test_errors_never_contain_secret_path_or_value(tmp_path):
    import traceback

    secret_file = tmp_path / "super-secret-marker-path"
    secret_file.write_bytes(b"\xff\xfe\x00invalid")
    values = enabled_stripe_env(tmp_path, KLYROW_STRIPE_SECRET_FILE=str(secret_file))
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    message = str(excinfo.value)
    formatted = "".join(traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__))
    assert str(secret_file) not in message
    assert str(secret_file) not in formatted
    assert excinfo.value.__cause__ is None


def test_capability_status_never_exposes_paths_keys_or_exceptions(tmp_path):
    settings = load_billing_settings(enabled_stripe_env(tmp_path))
    status = settings.capability_status().as_dict()
    serialized = str(status)
    assert str(tmp_path) not in serialized
    assert SYNTHETIC_SECRET not in serialized
    assert "Traceback" not in serialized


def test_disabled_capability_status_shape():
    settings = load_billing_settings({})
    status = settings.capability_status().as_dict()
    assert status == {
        "billing": {
            "enabled": False,
            "live_charging": False,
            "providers": {"stripe": "disabled", "paypal": "disabled", "stablecoin": "disabled"},
            "blocked_reasons": ["billing_disabled"],
        }
    }


def test_configuration_import_has_no_side_effects():
    before = set(os.environ.items())
    load_billing_settings(env())
    after = set(os.environ.items())
    assert before == after


def test_disabled_billing_never_opens_a_secret_file(tmp_path, monkeypatch):
    sentinel = tmp_path / "must-not-be-opened"
    sentinel.write_text(SYNTHETIC_SECRET)
    real_open = open
    opened = []

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    settings = load_billing_settings(env(KLYROW_STRIPE_SECRET_FILE=str(sentinel)))
    assert settings.enabled is False
    assert str(sentinel) not in opened


# ---- PayPal ----

def paypal_env(tmp_path, **overrides):
    values = env(
        KLYROW_BILLING_ENABLED="true",
        KLYROW_PAYPAL_ENABLED="true",
        KLYROW_PAYPAL_CLIENT_ID="sandbox-client-id",
        KLYROW_PAYPAL_SECRET_FILE=write_secret(tmp_path, "paypal-secret"),
    )
    values.update(overrides)
    return values


def test_paypal_requires_client_id(tmp_path):
    values = paypal_env(tmp_path)
    values["KLYROW_PAYPAL_CLIENT_ID"] = ""
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "paypal_client_id_missing"


def test_paypal_sandbox_defaults_to_official_api(tmp_path):
    settings = load_billing_settings(paypal_env(tmp_path))
    assert settings.paypal.api_base_url == "https://api-m.sandbox.paypal.com"
    assert settings.paypal.client_id == "sandbox-client-id"


def test_paypal_rejects_non_official_api_host(tmp_path):
    values = paypal_env(tmp_path, KLYROW_PAYPAL_API_BASE_URL="https://example.com")
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "paypal_api_base_url_invalid"


def test_paypal_webhook_processing_requires_registered_webhook_id_file(tmp_path):
    values = paypal_env(tmp_path, KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED="true")
    with pytest.raises(BillingConfigError, match="KLYROW_PAYPAL_WEBHOOK_SECRET_FILE"):
        load_billing_settings(values)


def test_paypal_production_requires_approval_currency_and_live_gate_inputs(tmp_path):
    values = paypal_env(
        tmp_path,
        KLYROW_PAYPAL_ENVIRONMENT="production",
        KLYROW_PAYPAL_API_BASE_URL="https://api-m.paypal.com",
        KLYROW_PAYPAL_PRODUCTION_APPROVED="true",
        KLYROW_PAYPAL_CURRENCY_ALLOWLIST="USD,EUR",
        KLYROW_PAYPAL_WEBHOOK_SECRET_FILE=write_secret(tmp_path, "paypal-webhook-id", "WH-PROD"),
    )
    settings = load_billing_settings(values)
    assert settings.paypal.environment == "production"
    assert settings.paypal.production_approved is True
    assert settings.paypal.currency_allowlist == ("USD", "EUR")


def test_stablecoin_rejects_private_key_secret(tmp_path):
    values = stablecoin_env(
        tmp_path,
        KLYROW_STABLECOIN_SECRET_FILE=write_secret(
            tmp_path,
            "stablecoin-private-key",
            "0x" + "1" * 64,
        ),
    )
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "stablecoin_private_key_rejected"


def test_stablecoin_requires_usd_allowlist(tmp_path):
    values = stablecoin_env(tmp_path, KLYROW_STABLECOIN_CURRENCY_ALLOWLIST="EUR")
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(values)
    assert excinfo.value.code == "stablecoin_usd_currency_required"


def test_stablecoin_requires_https_rpc_and_governed_wallet(tmp_path):
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(stablecoin_env(tmp_path, KLYROW_STABLECOIN_API_BASE_URL="http://rpc.example.test"))
    assert excinfo.value.code == "stablecoin_rpc_url_invalid"

    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(stablecoin_env(tmp_path, KLYROW_STABLECOIN_WALLET_REFERENCE=""))
    assert excinfo.value.code == "stablecoin_wallet_reference_missing"


def test_stablecoin_requires_usdc_six_decimals(tmp_path):
    with pytest.raises(BillingConfigError) as excinfo:
        load_billing_settings(stablecoin_env(tmp_path, KLYROW_STABLECOIN_DECIMALS="18"))
    assert excinfo.value.code == "stablecoin_invalid_decimals"
