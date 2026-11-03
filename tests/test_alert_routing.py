import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "alert_routing", ROOT / "scripts/render-alert-routing.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_private_route_preserves_scrapes_and_requires_mtls():
    source = yaml.safe_load((ROOT / "config/prometheus.yml").read_text())
    config = module.render(source, "10.20.30.40:9093", "aler.codestra.media", "production")
    assert config["scrape_configs"] == source["scrape_configs"]
    assert "alerting" not in source
    target = config["alerting"]["alertmanagers"][0]
    assert target["scheme"] == "https"
    assert target["tls_config"]["insecure_skip_verify"] is False
    assert target["tls_config"]["key_file"].startswith("/run/secrets/")
    assert config["global"]["external_labels"]["environment"] == "production"


@pytest.mark.parametrize(
    "target",
    [
        "37.27.128.39:9093",
        "localhost:9093",
        "127.0.0.1:9093",
        "10.20.30.40:0",
        "http://10.20.30.40:9093",
        "10.20.30.40:65536",
    ],
)
def test_public_or_invalid_targets_rejected(target):
    with pytest.raises(ValueError):
        module.render({}, target, "aler.codestra.media", "production")


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose unavailable")
def test_routed_composition_retains_scrapes_and_has_cross_host_network(tmp_path):
    base = tmp_path / "compose.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "prometheus": {"image": "prom/prometheus:v3.5.0", "networks": ["backend"]}
                },
                "networks": {"backend": {"internal": True}},
            }
        )
    )
    env = {
        **os.environ,
        **{
            key: str(tmp_path / key)
            for key in (
                "KLYROW_PROMETHEUS_ROUTED_CONFIG",
                "KLYROW_ALERTMANAGER_CA_FILE",
                "KLYROW_ALERTMANAGER_CLIENT_CERT_FILE",
                "KLYROW_ALERTMANAGER_CLIENT_KEY_FILE",
            )
        },
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "alert-routing-test",
            "-f",
            str(base),
            "-f",
            str(ROOT / "compose.alert-routing.yaml"),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    service = config["services"]["prometheus"]
    attached = service["networks"]
    assert "backend" in attached
    assert config["networks"]["backend"]["internal"] is True
    assert any(not config["networks"][name].get("internal", False) for name in attached)
    assert not service.get("ports")
    assert not service.get("privileged", False)
