import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
SOURCE = (ROOT / "apps/postal-provisioner/provisioner.rb").read_text(encoding="utf-8")
CONTRACT = yaml.safe_load(
    (ROOT / "openapi/postal-provisioner.openapi.yaml").read_text(encoding="utf-8")
)


def test_contract_has_only_the_private_provisioner_surface():
    assert set(CONTRACT["paths"]) == {
        "/healthz",
        "/v1/provision",
        "/v1/reconcile-inbound",
        "/v1/reconcile-outbound",
    }
    assert CONTRACT["servers"] == [
        {"url": "http://postal-provisioner:9090", "description": "Private Docker network only"}
    ]


def test_every_contract_route_is_implemented():
    for path in CONTRACT["paths"]:
        assert f'"{path}"' in SOURCE
    assert "def provision(payload)" in SOURCE
    assert "def reconcile_inbound(payload)" in SOURCE
    assert "def reconcile_outbound(payload)" in SOURCE


def test_mutations_require_constant_time_bearer_authentication():
    assert 'secure_token?(headers["authorization"])' in SOURCE
    assert "ActiveSupport::SecurityUtils.secure_compare" in SOURCE
    for path, item in CONTRACT["paths"].items():
        operation = next(iter(item.values()))
        if path == "/healthz":
            assert operation["security"] == []
        else:
            assert operation["security"] == [{"bearerAuth": []}]


def test_reconciliation_is_bounded_and_uses_postal_models():
    assert "domains.length > 100" in SOURCE
    assert "addresses.length > 300" in SOURCE
    assert "length > 16_384" in SOURCE
    assert "Domain.find_by!(name: name)" in SOURCE
    assert 'find_or_initialize_by(name: "Klyrow signed inbound adapter")' in SOURCE
    assert "find_or_create_by!(" in SOURCE
    assert "UPDATE " not in SOURCE.upper() and "INSERT INTO" not in SOURCE.upper()


def test_inbound_reconciliation_is_exact_address_bound_and_preserves_foreign_routes():
    inbound = SOURCE.split("def reconcile_inbound(payload)", 1)[1].split(
        "def reconcile_outbound(payload)", 1
    )[0]
    assert 'payload.fetch("addresses")' in inbound
    assert 'format: "Hash"' in inbound
    assert 'raise "foreign Postal route conflict"' in inbound
    assert "INBOUND_LOCAL_PARTS.each" not in inbound
    assert re.search(r"route\.server\s*=(?!=)", inbound) is None
    assert re.search(r"route\.endpoint\s*=(?!=)", inbound) is None
    assert "domain.routes.create!(" in inbound


def test_inbound_destination_is_pinned_to_the_gateway_contract():
    assert 'inbound_uri.scheme == "http"' in SOURCE
    assert 'inbound_uri.host == "gateway"' in SOURCE
    assert "inbound_uri.port == 8000" in SOURCE
    assert 'inbound_uri.path == "/v1/webhooks/postal-inbound"' in SOURCE


def test_provider_credentials_are_declared_write_only():
    api_key = CONTRACT["components"]["schemas"]["ProvisionResult"]["properties"]["api_key"]
    assert api_key["writeOnly"] is True
