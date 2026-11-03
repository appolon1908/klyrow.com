from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(path: str):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_security_smtp_override_wires_activation_gates_to_relay_and_worker():
    override = _load("compose.security-smtp.yaml")
    services = override["services"]
    relay = services["smtp-relay"]
    worker = services["worker"]

    assert relay["env_file"] == ["${KLYROW_ENV_FILE:-.env}"]
    required = {
        "KLYROW_SECURITY_SMTP_ENABLED": "${KLYROW_SECURITY_SMTP_ENABLED:-false}",
        "KLYROW_SECURITY_SMTP_LIVE_ENABLED": ("${KLYROW_SECURITY_SMTP_LIVE_ENABLED:-false}"),
        "KLYROW_SECURITY_SMTP_PRODUCTION_APPROVED": (
            "${KLYROW_SECURITY_SMTP_PRODUCTION_APPROVED:-false}"
        ),
        "KLYROW_SECURITY_SMTP_CANARY_RECIPIENTS": ("${KLYROW_SECURITY_SMTP_CANARY_RECIPIENTS:-}"),
        "KLYROW_SECURITY_SMTP_CANARY_MAX_DELIVERIES": (
            "${KLYROW_SECURITY_SMTP_CANARY_MAX_DELIVERIES:-1}"
        ),
    }
    for name, expected in required.items():
        assert relay["environment"][name] == expected
        assert worker["environment"][name] == expected

    assert worker["environment"]["KLYROW_SECURITY_SMTP_EXPECTED_MODE"] == (
        "${KLYROW_SECURITY_SMTP_EXPECTED_MODE:-disabled}"
    )
    assert "KLYROW_SECURITY_SMTP_PASSWORD" not in str(override)


def test_base_compose_keeps_security_smtp_private_and_general_delivery_disabled():
    base = _load("docker-compose.yml")
    relay = base["services"]["smtp-relay"]
    worker = base["services"]["worker"]
    gateway = base["services"]["gateway"]

    assert relay["ports"] == ["10.40.0.4:587:8025"]
    assert relay["environment"]["KLYROW_SMTP_TLS_CERT"] == "/run/klyrow/smtp.cert"
    assert relay["environment"]["KLYROW_SMTP_TLS_KEY"] == "/run/klyrow/smtp.key"
    assert worker["environment"]["LIVE_EMAIL_DELIVERY"] == "false"
    assert worker["environment"]["EXTERNAL_EMAIL_DELIVERY"] == "false"
    assert gateway["environment"]["LIVE_EMAIL_DELIVERY"] == "false"
    assert gateway["environment"]["EXTERNAL_EMAIL_DELIVERY"] == "false"


def test_example_environment_is_fail_closed():
    values = {}
    for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value

    assert values["KLYROW_SECURITY_SMTP_ENABLED"] == "false"
    assert values["KLYROW_SECURITY_SMTP_LIVE_ENABLED"] == "false"
    assert values["KLYROW_SECURITY_SMTP_PRODUCTION_APPROVED"] == "false"
    assert values["KLYROW_SECURITY_SMTP_CANARY_MAX_DELIVERIES"] == "1"
    assert values["KLYROW_SECURITY_SMTP_EXPECTED_MODE"] == "disabled"
