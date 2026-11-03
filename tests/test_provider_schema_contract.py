from pathlib import Path


def test_fresh_install_provider_smtp_contract_is_completed() -> None:
    published = Path("migrations/008_provider_saas_registry_separation.sql").read_text()
    reconciliation = Path(
        "migrations/2026082700_provider_smtp_credentials_contract.sql"
    ).read_text()

    # Migration 008 is already published and must remain immutable. The later
    # provider SMTP columns are carried by a forward-only reconciliation.
    assert "RENAME COLUMN verifier_hash TO secret_hash" in published
    for column in ("allowed_senders_json", "allowed_streams_json", "status"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" not in published
        assert f"ADD COLUMN IF NOT EXISTS {column}" in reconciliation
