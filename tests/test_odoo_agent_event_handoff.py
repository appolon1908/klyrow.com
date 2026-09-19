"""Check the repository-owned downstream contract, not a synthetic Odoo payload."""
import json
from pathlib import Path

from apps.gateway.app.delivery_safety import DISABLED_EMAIL_CONTROLS

ROOT = Path(__file__).resolve().parents[1]


def test_handoff_uses_the_checked_in_middleware_command_contract():
    contract = json.loads((ROOT / "codestra/integration/middleware-command-contract.v1.json").read_text())
    assert contract["authority"] == "Middleware-"
    commands = {item["type"]: item for item in contract["commands"]}
    assert commands["email.message.send.v1"]["deliveryDefault"] == "disabled"
    assert commands["email.message.send.v1"]["idempotencyKeyRequired"] is True
    assert "odoo" in contract["transport"]["forbiddenDirectTargets"]
    assert contract["invariants"]["workflowJsonMayContainCredentials"] is False
    assert contract["invariants"]["liveDeliveryEnabledByDefault"] is False


def test_email_activation_defaults_remain_fail_closed():
    assert DISABLED_EMAIL_CONTROLS == {
        "KLYROW_SAFE_MODE": True,
        "KLYROW_PRODUCTION_GATE_APPROVED": False,
        "LIVE_EMAIL_DELIVERY": False,
        "EXTERNAL_EMAIL_DELIVERY": False,
        "PRODUCTION_PROVIDER_ROUTING": False,
    }
