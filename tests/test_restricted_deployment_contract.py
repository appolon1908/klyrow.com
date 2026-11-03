import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_restricted_deployment_contract_is_fail_closed_and_preserves_data():
    contract = json.loads((ROOT / "deploy/restricted-deployment-contract.json").read_text())
    assert contract["compose_project"] == "klyrow"
    assert contract["required_source_sha"] == "24619abe4e8b1b3f2c231bca5d138dd45c014a07"
    assert contract["fixed_containers"] == {
        "klyrow-gateway-1": "gateway",
        "klyrow-worker-1": "worker",
        "klyrow-smtp-relay-1": "smtp-relay",
        "klyrow-postgres-1": "postgres",
    }
    assert set(contract["preserved_volumes"]) >= {
        "klyrow_postgres_data",
        "klyrow_postal_db",
        "klyrow_postal_assets",
    }
    assert contract["expected_environment"] == {
        "KLYROW_SAFE_MODE": "true",
        "KLYROW_PRODUCTION_GATE_APPROVED": "false",
        "LIVE_EMAIL_DELIVERY": "false",
    }
    assert contract["expected_readback"] == {
        "safe_mode": True,
        "production_gate_approved": False,
        "production_gate_open": False,
        "live_email_delivery": False,
        "outbox_active": 0,
    }
