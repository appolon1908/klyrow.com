import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "deploy" / "staging"


def test_staging_images_are_required_as_immutable_digests():
    script = (ROOT / "scripts" / "staging-preflight").read_text()
    example = (STAGING / "staging.env.example").read_text()
    assert "@sha256:" in script
    for image in ("GATEWAY", "WEB", "MIGRATE", "SEARCH", "CADDY", "POSTGRES", "PROMETHEUS", "GRAFANA", "NODE_EXPORTER"):
        assert f"KLYROW_{image}_IMAGE" in script
        assert f"KLYROW_{image}_IMAGE=" in example


def test_only_caddy_publishes_public_ports():
    compose = yaml.safe_load((STAGING / "compose.yml").read_text())
    assert compose["services"]["edge"]["ports"] == ["80:80", "443:443"]
    for name, service in compose["services"].items():
        if name != "edge":
            assert "ports" not in service, name
    assert compose["networks"]["data"]["internal"] is True
    assert compose["networks"]["observability"]["internal"] is True
    caddy = (STAGING / "Caddyfile").read_text()
    assert "grafana:" not in caddy
    assert "prometheus:" not in caddy
    assert "header_up Host app.klyrow.com" in caddy
    for header in ("Client", "Tenant", "Role"):
        assert f"request_header -X-Authenticated-{header}" in caddy


def test_staging_is_fail_closed_for_external_delivery():
    compose = yaml.safe_load((STAGING / "compose.yml").read_text())
    env = compose["services"]["gateway"]["environment"]
    assert env["KLYROW_ENV"] == "production"
    assert env["KLYROW_IDENTITY_PROFILE"] == "staging"
    assert env["KLYROW_LOCAL_AUTH_ENABLED"] == "false"
    assert env["KLYROW_SAFE_MODE"] == "true"
    assert env["LIVE_EMAIL_DELIVERY"] == "false"
    assert env["EXTERNAL_EMAIL_DELIVERY"] == "false"
    assert env["PRODUCTION_PROVIDER_ROUTING"] == "false"
    assert env["KLYROW_EMAIL_EVENT_URL"] == "${KLYROW_EMAIL_EVENT_URL:?required}"
    assert env["KLYROW_PROVIDER_CREDENTIAL_KEY_FILE"] == "/run/secrets/provider_credential_key"


def test_keycloak_client_requires_pkce_and_disables_password_grant():
    realm = json.loads((STAGING / "keycloak" / "klyrow-staging-client.json").read_text())
    client = next(item for item in realm["clients"] if item["clientId"] == "klyrow-staging-portal")
    assert realm["realm"] == "codestra"
    assert realm["status"] == "prepared-not-applied"
    assert client["publicClient"] is True
    assert client["standardFlowEnabled"] is True
    assert client["directAccessGrantsEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"


def test_openbao_policy_explicitly_denies_odoo_writer_secret():
    policy = (STAGING / "openbao" / "klyrow-staging.hcl").read_text()
    assert re.search(r'odoo-writer/\*"\s*\{\s*capabilities\s*=\s*\["deny"\]', policy, re.S)


def test_staging_dependencies_are_declared():
    compose = yaml.safe_load((STAGING / "compose.yml").read_text())
    template = (STAGING / "staging.env.example").read_text()
    for service in compose["services"].values():
        variable = service["image"].split("${", 1)[1].split(":", 1)[0]
        assert variable + "=" in template
    gateway = compose["services"]["gateway"]
    for name in ("webhook_secret", "provider_credential_key", "middleware_ca", "middleware_cert", "middleware_key"):
        assert name in gateway["secrets"]
        assert name in compose["secrets"]
    prometheus = compose["services"]["prometheus"]
    assert {"source": "metrics_token", "target": "klyrow_metrics_token"} in prometheus["secrets"]
    assert any("/etc/prometheus/alerts.yml" in value for value in prometheus["volumes"])
    assert "header_up Host app.klyrow.com" in (STAGING / "Caddyfile").read_text()


def test_search_bridges_its_file_backed_secret_to_the_supported_setting():
    compose = yaml.safe_load((STAGING / "compose.yml").read_text())
    search = compose["services"]["search"]
    assert "MEILI_MASTER_KEY_FILE" not in search.get("environment", {})
    assert "cat /run/secrets/search_key" in search["command"][0]


def test_node_exporter_reads_the_host_without_publishing_its_native_port():
    compose = yaml.safe_load((STAGING / "compose.yml").read_text())
    exporter = compose["services"]["node-exporter"]
    assert exporter["command"] == ["--path.rootfs=/host"]
    assert "/:/host:ro,rslave" in exporter["volumes"]
    assert exporter["networks"] == ["observability"]
    assert "ports" not in exporter
