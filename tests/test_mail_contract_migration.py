from pathlib import Path


MIGRATION = Path("migrations/2026090101_mail_contract_and_command_plane.sql")


def test_mail_contract_migration_is_forward_only_and_fail_closed():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS priority" in source
    assert "email_outbox_priority_claim" in source
    assert "LEGACY_UNVERIFIED" in source
    assert "SET disposition = 'QUARANTINE'" in source
    for field in ("auth_verdict", "spf_result", "dkim_result", "dmarc_result", "arc_result", "dmarc_fail_action"):
        assert "ADD COLUMN IF NOT EXISTS " + field in source
    assert "CREATE TABLE IF NOT EXISTS middleware_command_operations" in source
    assert "uq_middleware_command_tenant_idempotency" in source
    assert "DROP TABLE" not in source
    assert "TRUNCATE" not in source


def test_production_compose_requires_latest_mail_contract_migration():
    required = "KLYROW_REQUIRED_SCHEMA_VERSION: 2026091302_m07_business_event_runtime.sql"
    assert required in Path("docker-compose.yml").read_text(encoding="utf-8")
    assert required in Path("docker-compose.postal-provisioning.yml").read_text(encoding="utf-8")
