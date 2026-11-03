"""Fail-closed production email delivery contract."""

import os
from collections.abc import Mapping

DISABLED_EMAIL_CONTROLS = {
    "KLYROW_SAFE_MODE": True,
    "KLYROW_PRODUCTION_GATE_APPROVED": False,
    "LIVE_EMAIL_DELIVERY": False,
    "EXTERNAL_EMAIL_DELIVERY": False,
    "PRODUCTION_PROVIDER_ROUTING": False,
}
LIVE_EMAIL_CONTROLS = {name: not value for name, value in DISABLED_EMAIL_CONTROLS.items()}


def email_activation_status(environment: Mapping[str, str] | None = None) -> dict:
    """Describe this process's general-email controls without claiming delivery.

    Missing controls use disabled defaults. Invalid values always close the gate;
    accepting only explicit true/false prevents typos from becoming activation.
    SECURITY SMTP retains its separate, recipient-scoped activation contract.
    """

    source = os.environ if environment is None else environment
    controls = {}
    missing = []
    invalid = []
    for name, default in DISABLED_EMAIL_CONTROLS.items():
        if name not in source:
            missing.append(name)
            controls[name] = default
            continue
        raw = source[name]
        value = raw.strip().lower() if isinstance(raw, str) else None
        if value not in {"true", "false"}:
            invalid.append(name)
            controls[name] = None
        else:
            controls[name] = value == "true"
    blockers = [
        name for name, expected in LIVE_EMAIL_CONTROLS.items() if controls[name] != expected
    ]
    enabled = not missing and not invalid and not blockers
    return {
        "scope": "process_general_email",
        "mode": "live_eligible" if enabled else "disabled",
        "live_delivery_enabled": enabled,
        "safe_mode": not enabled,
        "controls": controls,
        "missing_controls": missing,
        "invalid_controls": invalid,
        "blocking_controls": blockers,
        "stack_consistency": "unverified",
        "end_to_end_delivery": "unverified",
    }


def live_email_delivery_enabled() -> bool:
    """Require every independent delivery control to opt in explicitly."""

    return email_activation_status()["live_delivery_enabled"]


def safe_mode_enabled() -> bool:
    return not live_email_delivery_enabled()
