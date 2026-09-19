"""Phase 0 fail-closed billing activation configuration tests.

Scope: configuration parsing and validation only. No PaymentAttempt, provider
adapter, ledger, or webhook runtime is exercised or implied by these tests.
"""
import os

import pytest

from apps.gateway.app.billing_activation import (
    FLAG_NAMES,
    SECRET_REFERENCE_NAMES,
    BillingActivationError,
    validate_billing_activation,
)

ALL_FLAGS = list(FLAG_NAMES.values())


def env(**overrides):
    base = {name: "false" for name in ALL_FLAGS}
    base.update(overrides)
    return base


def test_every_flag_defaults_safely_when_environment_is_empty():
    state = validate_billing_activation({})
    assert state.core_enabled is False
    assert state.entitlements_enabled is False
    assert state.provider_webhooks_enabled is False
    assert state.stripe_enabled is False
    assert state.paypal_enabled is False
    assert state.stablecoin_enabled is False
    assert state.dunning_enabled is False
    assert state.live_charging_enabled is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_boolean_parsing_accepts_documented_true_forms(value):
    state = validate_billing_activation(env(KLYROW_BILLING_CORE_ENABLED=value))
    assert state.core_enabled is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_boolean_parsing_accepts_documented_false_forms(value):
    state = validate_billing_activation(env(KLYROW_BILLING_CORE_ENABLED=value))
    assert state.core_enabled is False


@pytest.mark.parametrize("value", ["", "enabled", "2", "yesplease", " "])
def test_invalid_boolean_values_fail_closed(value):
    with pytest.raises(BillingActivationError):
        validate_billing_activation(env(KLYROW_BILLING_CORE_ENABLED=value))


def test_provider_activation_requires_billing_core():
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_CORE_ENABLED"):
        validate_billing_activation(env(KLYROW_BILLING_STRIPE_ENABLED="true"))


def test_entitlements_requires_billing_core():
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_CORE_ENABLED"):
        validate_billing_activation(env(KLYROW_BILLING_ENTITLEMENTS_ENABLED="true"))


def test_dunning_requires_billing_core():
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_CORE_ENABLED"):
        validate_billing_activation(env(KLYROW_BILLING_DUNNING_ENABLED="true"))


def test_live_charging_requires_billing_core():
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_CORE_ENABLED"):
        validate_billing_activation(env(KLYROW_BILLING_LIVE_CHARGING_ENABLED="true"))


def test_disabled_configuration_starts_successfully():
    state = validate_billing_activation(env())
    assert state.core_enabled is False


def test_stripe_activation_requires_secret_reference(tmp_path):
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_STRIPE_SECRET_FILE"):
        validate_billing_activation(
            env(KLYROW_BILLING_CORE_ENABLED="true", KLYROW_BILLING_STRIPE_ENABLED="true")
        )


def test_missing_secret_reference_fails(tmp_path):
    with pytest.raises(BillingActivationError):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(tmp_path / "does-not-exist"),
            )
        )


def test_empty_secret_reference_value_fails():
    with pytest.raises(BillingActivationError):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE="",
            )
        )


def test_empty_secret_file_fails(tmp_path):
    secret_file = tmp_path / "stripe-secret"
    secret_file.write_text("   ")
    with pytest.raises(BillingActivationError):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
            )
        )


def test_unavailable_secret_file_fails(tmp_path):
    # A directory path is a readable filesystem entry but not a readable secret file.
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(BillingActivationError):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(directory),
            )
        )


def test_valid_stripe_secret_reference_activates_provider(tmp_path):
    secret_file = tmp_path / "stripe-secret"
    secret_file.write_text("sk_test_placeholder")
    state = validate_billing_activation(
        env(
            KLYROW_BILLING_CORE_ENABLED="true",
            KLYROW_BILLING_STRIPE_ENABLED="true",
            KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
        )
    )
    assert state.stripe_enabled is True
    assert state.active_providers == ("stripe",)


def test_provider_webhooks_require_an_enabled_provider(tmp_path):
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED"):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED="true",
            )
        )


def test_provider_webhooks_require_verification_secret_reference(tmp_path):
    secret_file = tmp_path / "stripe-secret"
    secret_file.write_text("sk_test_placeholder")
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_STRIPE_WEBHOOK_SECRET_FILE"):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
                KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED="true",
            )
        )


def test_provider_webhooks_activate_with_all_secret_references(tmp_path):
    secret_file = tmp_path / "stripe-secret"
    secret_file.write_text("sk_test_placeholder")
    webhook_file = tmp_path / "stripe-webhook-secret"
    webhook_file.write_text("whsec_placeholder")
    state = validate_billing_activation(
        env(
            KLYROW_BILLING_CORE_ENABLED="true",
            KLYROW_BILLING_STRIPE_ENABLED="true",
            KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
            KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED="true",
            KLYROW_BILLING_STRIPE_WEBHOOK_SECRET_FILE=str(webhook_file),
        )
    )
    assert state.provider_webhooks_enabled is True


def test_dunning_requires_entitlements_even_with_core_enabled():
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_ENTITLEMENTS_ENABLED"):
        validate_billing_activation(
            env(KLYROW_BILLING_CORE_ENABLED="true", KLYROW_BILLING_DUNNING_ENABLED="true")
        )


def test_dunning_activates_with_entitlements_enabled():
    state = validate_billing_activation(
        env(
            KLYROW_BILLING_CORE_ENABLED="true",
            KLYROW_BILLING_ENTITLEMENTS_ENABLED="true",
            KLYROW_BILLING_DUNNING_ENABLED="true",
        )
    )
    assert state.dunning_enabled is True


def test_live_charging_requires_a_valid_provider(tmp_path):
    with pytest.raises(BillingActivationError, match="KLYROW_BILLING_LIVE_CHARGING_ENABLED"):
        validate_billing_activation(
            env(KLYROW_BILLING_CORE_ENABLED="true", KLYROW_BILLING_LIVE_CHARGING_ENABLED="true")
        )


def test_live_charging_activates_with_a_valid_provider(tmp_path):
    secret_file = tmp_path / "stripe-secret"
    secret_file.write_text("sk_test_placeholder")
    state = validate_billing_activation(
        env(
            KLYROW_BILLING_CORE_ENABLED="true",
            KLYROW_BILLING_STRIPE_ENABLED="true",
            KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
            KLYROW_BILLING_LIVE_CHARGING_ENABLED="true",
        )
    )
    assert state.live_charging_enabled is True


def test_errors_do_not_expose_secret_path_or_content(tmp_path):
    secret_file = tmp_path / "super-secret-stripe-key-value"
    secret_file.write_text("sk_live_should_never_appear_in_error_text")
    directory = tmp_path / "unreadable"
    directory.mkdir()
    with pytest.raises(BillingActivationError) as excinfo:
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(directory),
            )
        )
    message = str(excinfo.value)
    assert str(directory) not in message
    assert "sk_live_should_never_appear_in_error_text" not in message
    assert "KLYROW_BILLING_STRIPE_SECRET_FILE" in message


def test_configuration_import_has_no_side_effects():
    # Re-importing/parsing must not create files, sockets, or provider clients.
    before = set(os.environ.items())
    validate_billing_activation(env())
    after = set(os.environ.items())
    assert before == after


def test_invalid_enabled_configuration_blocks_gateway_startup():
    from apps.gateway.app.main import validate_billing_activation_on_startup

    original = {name: os.environ.get(name) for name in ALL_FLAGS}
    try:
        os.environ["KLYROW_BILLING_CORE_ENABLED"] = "false"
        os.environ["KLYROW_BILLING_STRIPE_ENABLED"] = "true"
        with pytest.raises(RuntimeError):
            validate_billing_activation_on_startup()
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_fully_disabled_configuration_permits_gateway_startup():
    from apps.gateway.app.main import validate_billing_activation_on_startup

    original = {name: os.environ.get(name) for name in ALL_FLAGS}
    try:
        for name in ALL_FLAGS:
            os.environ.pop(name, None)
        validate_billing_activation_on_startup()
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_secret_reference_names_cover_every_provider_and_webhook():
    expected = {
        "stripe",
        "stripe_webhook",
        "paypal",
        "paypal_webhook",
        "stablecoin",
        "stablecoin_webhook",
    }
    assert set(SECRET_REFERENCE_NAMES) == expected
