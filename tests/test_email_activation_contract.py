import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.gateway.app import main, service_worker
from apps.gateway.app.delivery_safety import DISABLED_EMAIL_CONTROLS, LIVE_EMAIL_CONTROLS
from apps.gateway.app.email_activation_contract import EMAIL_COMPONENTS, validate_email_activation

ROOT = Path(__file__).resolve().parents[1]


def environment(live=False):
    profile = LIVE_EMAIL_CONTROLS if live else DISABLED_EMAIL_CONTROLS
    return {name: str(value).lower() for name, value in profile.items()}


def compose(live=False):
    return {"services": {name: {"environment": environment(live)} for name in EMAIL_COMPONENTS}}


def runtime(live=False):
    return [
        {
            "Name": "/klyrow-" + name + "-1",
            "Config": {"Env": [k + "=" + v for k, v in environment(live).items()]},
            "State": {"Status": "running"},
        }
        for name in EMAIL_COMPONENTS
    ]


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("source,fixture", [("compose", compose), ("inspect", runtime)])
def test_complete_profiles_pass_without_claiming_provider_delivery(live, source, fixture):
    result = validate_email_activation(fixture(live), source=source)
    assert result["configuration_consistent"] is True
    assert result["mode"] == ("live_eligible" if live else "disabled")
    assert result["postal_enforcement"] == "unverified"
    assert result["end_to_end_delivery"] == "unverified"


@pytest.mark.parametrize("name", EMAIL_COMPONENTS)
def test_any_component_drift_blocks_deployment(name):
    document = compose(True)
    document["services"][name]["environment"] = environment(False)
    result = validate_email_activation(document)
    assert result["configuration_consistent"] is False
    assert result["mode"] == "blocked"


def test_observed_server37_split_is_rejected():
    document = runtime(True)
    for container in document:
        if "postal-" in container["Name"]:
            container["Config"]["Env"] = ["LIVE_EMAIL_DELIVERY=false"]
    result = validate_email_activation(document, source="inspect")
    assert result["configuration_consistent"] is False
    assert "postal-worker:gateway_mismatch" in result["errors"]
    assert "postal-smtp:missing_controls" in result["errors"]


def test_uniform_but_partial_activation_is_not_a_valid_profile():
    document = compose(True)
    for service in document["services"].values():
        service["environment"]["EXTERNAL_EMAIL_DELIVERY"] = "false"
    result = validate_email_activation(document)
    assert result["configuration_consistent"] is False
    assert "worker:partial_activation" in result["errors"]


def test_missing_and_duplicate_containers_are_rejected():
    document = runtime()
    document.pop()
    document.append(document[0])
    result = validate_email_activation(document, source="inspect")
    assert "postal-smtp:missing_component" in result["errors"]
    assert "gateway:duplicate_container" in result["errors"]


def test_stopped_container_cannot_certify_runtime():
    document = runtime()
    document[1]["State"]["Status"] = "exited"
    result = validate_email_activation(document, source="inspect")
    assert result["configuration_consistent"] is False
    assert "worker:not_running" in result["errors"]


def test_duplicate_control_and_sensitive_values_are_not_reported():
    document = runtime()
    document[0]["Config"]["Env"] += ["LIVE_EMAIL_DELIVERY=sensitive", "TOKEN=private"]
    result = validate_email_activation(document, source="inspect")
    assert "gateway:invalid_environment" in result["errors"]
    assert "sensitive" not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_base_compose_has_consistent_disabled_controls():
    document = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    result = validate_email_activation(document)
    assert result["configuration_consistent"] is True, result["errors"]
    assert result["mode"] == "disabled"


def test_shared_overlay_updates_every_component_and_preserves_unrelated_settings():
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    overlay = yaml.safe_load((ROOT / "compose.email-activation.yaml").read_text())
    assert set(overlay["services"]) == set(EMAIL_COMPONENTS)
    expected = overlay["services"]["gateway"]["environment"]
    for name in EMAIL_COMPONENTS:
        assert overlay["services"][name] == {"environment": expected}
    # Resolve the five explicit substitutions as Compose would for a live input.
    for service in overlay["services"].values():
        assert set(service["environment"]) == set(LIVE_EMAIL_CONTROLS)
        for key, value in service["environment"].items():
            assert value == "${" + key + ":-" + str(DISABLED_EMAIL_CONTROLS[key]).lower() + "}"
    for name in EMAIL_COMPONENTS:
        base["services"][name].setdefault("environment", {}).update(environment(True))
    assert validate_email_activation(base)["configuration_consistent"] is True
    assert base["services"]["gateway"]["environment"]["LIVE_SMS_DELIVERY"] == "false"


@pytest.mark.parametrize(
    "document,code", [(compose(), 0), (compose(True), 0), ({"services": {}}, 1)]
)
def test_cli_returns_machine_readable_result(document, code):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify-email-activation")],
        input=json.dumps(document),
        capture_output=True,
        text=True,
    )
    assert result.returncode == code, result.stderr
    assert json.loads(result.stdout)["configuration_consistent"] == (code == 0)


def test_cli_does_not_echo_malformed_sensitive_input():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify-email-activation")],
        input='{"secret": "PRIVATE',
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "PRIVATE" not in result.stdout + result.stderr


def test_activation_api_requires_platform_admin(monkeypatch):
    monkeypatch.setattr(
        main.app, "dependency_overrides", {main.auth: lambda: {"role": "tenant_admin"}}
    )
    client = TestClient(main.app)
    assert client.get("/v1/admin/delivery/activation").status_code == 403
    main.app.dependency_overrides[main.auth] = lambda: {"role": "platform_admin"}
    for key, value in environment(True).items():
        monkeypatch.setenv(key, value)
    response = client.get("/v1/admin/delivery/activation")
    assert response.status_code == 200
    assert response.json()["live_delivery_enabled"] is True
    monkeypatch.setenv("EXTERNAL_EMAIL_DELIVERY", "false")
    blocked = client.get("/v1/admin/delivery/activation").json()
    assert blocked["live_delivery_enabled"] is False
    assert "EXTERNAL_EMAIL_DELIVERY" in blocked["blocking_controls"]
    assert client.post("/v1/admin/delivery/activation", json={"enabled": True}).status_code == 405


def test_capabilities_do_not_claim_external_delivery_with_a_closed_control(monkeypatch):
    monkeypatch.setattr(main, "SAFE_MODE", False)
    for key, value in environment(True).items():
        monkeypatch.setenv(key, value)
    assert main.capabilities()["external_delivery_enabled"] is True
    monkeypatch.setenv("PRODUCTION_PROVIDER_ROUTING", "false")
    assert main.capabilities()["external_delivery_enabled"] is False


def test_worker_health_reports_its_own_activation_without_sending_mail(monkeypatch):
    import asyncio

    class Reader:
        async def read(self, _):
            return b"GET / HTTP/1.1\r\n\r\n"

    class Writer:
        def write(self, value):
            self.value = value

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    writer = Writer()
    asyncio.run(service_worker.health(Reader(), writer))
    body = json.loads(writer.value.split(b"\r\n\r\n", 1)[1])
    assert body["status"] == "ok"
    assert body["email_activation"]["mode"] == "disabled"
