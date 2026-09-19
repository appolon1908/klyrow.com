"""Phase 0 fail-closed billing activation configuration tests.

Scope: configuration parsing and validation only. No PaymentAttempt, provider
adapter, ledger, or webhook runtime is exercised or implied by these tests.

Fixture secret values are deliberately NOT shaped like real provider tokens
(no `sk_`/`whsec_`-style prefixes) so secret scanners do not flag this test
file; the validator only checks non-emptiness/UTF-8/size, never token shape.
"""
import os
import traceback

import pytest

from apps.gateway.app.billing_activation import (
    FLAG_NAMES,
    SECRET_REFERENCE_NAMES,
    _MAX_SECRET_BYTES,
    BillingActivationError,
    validate_billing_activation,
)

SYNTHETIC_SECRET = "SYNTHETIC-FIXTURE-VALUE-NOT-A-CREDENTIAL-0000111122223333"
SYNTHETIC_WEBHOOK_SECRET = "SYNTHETIC-FIXTURE-WEBHOOK-VALUE-NOT-A-CREDENTIAL-4444555566667777"

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
    secret_file.write_text(SYNTHETIC_SECRET)
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
    secret_file.write_text(SYNTHETIC_SECRET)
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
    secret_file.write_text(SYNTHETIC_SECRET)
    webhook_file = tmp_path / "stripe-webhook-secret"
    webhook_file.write_text(SYNTHETIC_WEBHOOK_SECRET)
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
    secret_file.write_text(SYNTHETIC_SECRET)
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
    secret_file.write_text("SYNTHETIC-FIXTURE-MUST-NEVER-APPEAR-IN-ERROR-TEXT-99887766")
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
    assert "SYNTHETIC-FIXTURE-MUST-NEVER-APPEAR-IN-ERROR-TEXT-99887766" not in message
    assert "KLYROW_BILLING_STRIPE_SECRET_FILE" in message


def test_missing_secret_file_traceback_does_not_leak_path(tmp_path):
    # B3: the formatted, chained traceback must not leak the path either.
    missing = tmp_path / "does-not-exist-secret"
    with pytest.raises(BillingActivationError) as excinfo:
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(missing),
            )
        )
    formatted = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert str(missing) not in formatted
    assert excinfo.value.__cause__ is None


def test_secret_file_at_exact_byte_limit_is_accepted(tmp_path):
    secret_file = tmp_path / "exact-limit-secret"
    secret_file.write_bytes(b"a" * _MAX_SECRET_BYTES)
    state = validate_billing_activation(
        env(
            KLYROW_BILLING_CORE_ENABLED="true",
            KLYROW_BILLING_STRIPE_ENABLED="true",
            KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
        )
    )
    assert state.stripe_enabled is True


def test_secret_file_one_byte_over_limit_is_rejected(tmp_path):
    secret_file = tmp_path / "one-over-secret"
    secret_file.write_bytes(b"a" * (_MAX_SECRET_BYTES + 1))
    with pytest.raises(BillingActivationError, match="oversized"):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
            )
        )


def test_secret_file_multibyte_over_limit_is_rejected_without_full_read(tmp_path):
    # Large multibyte content: must be rejected via the bounded read, not by
    # decoding/measuring the whole (much larger) file.
    secret_file = tmp_path / "multibyte-over-secret"
    secret_file.write_text("\u00e9" * 40000, encoding="utf-8")  # 80000 bytes
    with pytest.raises(BillingActivationError, match="oversized"):
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
            )
        )


def test_malformed_utf8_secret_file_fails_closed_without_leaking(tmp_path):
    secret_file = tmp_path / "invalid-encoding-secret"
    secret_file.write_bytes(b"\xff\xfe\x00invalid")
    with pytest.raises(BillingActivationError, match="UTF-8") as excinfo:
        validate_billing_activation(
            env(
                KLYROW_BILLING_CORE_ENABLED="true",
                KLYROW_BILLING_STRIPE_ENABLED="true",
                KLYROW_BILLING_STRIPE_SECRET_FILE=str(secret_file),
            )
        )
    assert excinfo.value.__cause__ is None


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


def test_billing_validation_is_registered_before_worker_scheduling_startup_hooks():
    """B2: the gateway must not schedule workers before billing validation runs."""
    from apps.gateway.app.main import app, validate_billing_activation_on_startup

    handlers = list(app.router.on_startup)
    assert validate_billing_activation_on_startup in handlers
    billing_index = handlers.index(validate_billing_activation_on_startup)
    worker_hook_names = {
        "start_provider_worker",
        "start_postal_retry_worker",
        "reconcile_provider_registry_on_startup",
    }
    later_worker_hooks = [
        index for index, handler in enumerate(handlers) if handler.__name__ in worker_hook_names
    ]
    assert later_worker_hooks, "expected at least one worker-scheduling startup hook to be present"
    assert all(billing_index < index for index in later_worker_hooks)


def test_disabled_billing_never_reads_a_secret_file(tmp_path, monkeypatch):
    """Disabled-by-default configuration must not touch any secret reference."""
    sentinel = tmp_path / "must-not-be-read"
    sentinel.write_text("SYNTHETIC-SENTINEL-VALUE")
    real_open = open
    opened_paths = []

    def spying_open(file, *args, **kwargs):
        opened_paths.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spying_open)
    state = validate_billing_activation(env(KLYROW_BILLING_STRIPE_SECRET_FILE=str(sentinel)))
    assert state.core_enabled is False
    assert str(sentinel) not in opened_paths
