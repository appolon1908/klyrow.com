import pytest

from apps.gateway.app.delivery_safety import (
    LIVE_EMAIL_CONTROLS,
    email_activation_status,
    live_email_delivery_enabled,
    safe_mode_enabled,
)


def set_controls(monkeypatch, *, safe_mode, approved, live_delivery):
    monkeypatch.setenv("KLYROW_SAFE_MODE", safe_mode)
    monkeypatch.setenv("KLYROW_PRODUCTION_GATE_APPROVED", approved)
    monkeypatch.setenv("LIVE_EMAIL_DELIVERY", live_delivery)
    monkeypatch.setenv("EXTERNAL_EMAIL_DELIVERY", "true")
    monkeypatch.setenv("PRODUCTION_PROVIDER_ROUTING", "true")


def test_safe_mode_blocks_delivery_even_with_other_controls_enabled(monkeypatch):
    set_controls(monkeypatch, safe_mode="true", approved="true", live_delivery="true")
    assert safe_mode_enabled() is True
    assert live_email_delivery_enabled() is False


def test_disabled_delivery_flag_blocks_delivery(monkeypatch):
    set_controls(monkeypatch, safe_mode="false", approved="true", live_delivery="false")
    assert safe_mode_enabled() is True
    assert live_email_delivery_enabled() is False


def test_gate_approval_alone_cannot_open_delivery(monkeypatch):
    set_controls(monkeypatch, safe_mode="true", approved="true", live_delivery="false")
    assert safe_mode_enabled() is True
    assert live_email_delivery_enabled() is False


def test_delivery_requires_all_five_explicit_controls(monkeypatch):
    set_controls(monkeypatch, safe_mode="false", approved="true", live_delivery="true")
    assert safe_mode_enabled() is False
    assert live_email_delivery_enabled() is True


@pytest.mark.parametrize("name", list(LIVE_EMAIL_CONTROLS))
@pytest.mark.parametrize("value", [None, "", "invalid", "1", "false"])
def test_each_control_can_close_the_live_gate(monkeypatch, name, value):
    for control, expected in LIVE_EMAIL_CONTROLS.items():
        monkeypatch.setenv(control, str(expected).lower())
    if value is None:
        monkeypatch.delenv(name)
    else:
        # For the inverse SAFE_MODE switch, true is the blocking value.
        monkeypatch.setenv(
            name, "true" if name == "KLYROW_SAFE_MODE" and value == "false" else value
        )
    assert live_email_delivery_enabled() is False
    assert safe_mode_enabled() is True


def test_normalized_controls_and_unrelated_environment_never_leak():
    env = {
        name: "  " + str(expected).upper() + "  " for name, expected in LIVE_EMAIL_CONTROLS.items()
    }
    env["SECRET_TOKEN"] = "do-not-report-this-value"
    result = email_activation_status(env)
    assert result["live_delivery_enabled"] is True
    assert result["mode"] == "live_eligible"
    assert result["end_to_end_delivery"] == "unverified"
    assert "do-not-report-this-value" not in str(result)


def test_missing_and_invalid_controls_are_explained_without_echoing_values():
    result = email_activation_status({"LIVE_EMAIL_DELIVERY": "sensitive-invalid-value"})
    assert result["live_delivery_enabled"] is False
    assert "KLYROW_SAFE_MODE" in result["missing_controls"]
    assert result["invalid_controls"] == ["LIVE_EMAIL_DELIVERY"]
    assert result["controls"]["LIVE_EMAIL_DELIVERY"] is None
    assert "sensitive-invalid-value" not in str(result)
