import os
import subprocess
import sys
from pathlib import Path

from apps.gateway.app import service_worker

ROOT = Path(__file__).parents[1]


def test_worker_module_is_importable_as_process_entrypoint():
    env = os.environ.copy()
    for name in (
        "KLYROW_SESSION_SECRET_FILE",
        "KLYROW_DATABASE_PASSWORD_FILE",
        "KLYROW_REQUIRED_SCHEMA_VERSION",
    ):
        env.pop(name, None)
    env.update(
        KLYROW_ENV="test",
        KLYROW_DATABASE_URL="sqlite://",
        KLYROW_SESSION_SECRET="worker-entrypoint-test-secret-value-0000000000000000",
    )
    result = subprocess.run(
        [sys.executable, "-c", "import apps.gateway.app.service_worker"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_base_worker_preserves_legacy_delivery_without_complete_provisioning_stack(
    monkeypatch,
):
    monkeypatch.delenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", raising=False)
    assert service_worker.tenant_postal_provisioning_enabled() is False
    assert service_worker.selected_email_outbox_loop() is service_worker.email_outbox_loop


def test_optional_provisioning_stack_selects_tenant_credentials(monkeypatch):
    monkeypatch.setenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "true")
    assert service_worker.tenant_postal_provisioning_enabled() is True
    assert service_worker.selected_email_outbox_loop() is service_worker.tenant_email_outbox_loop


def test_compose_contract_cannot_select_tenant_loop_without_required_services():
    base = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    override = (ROOT / "docker-compose.postal-provisioning.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED" not in base
    assert 'KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED: "true"' in override
    assert "postal-provisioner:" in override
    assert "provisioning-worker:" in override
    assert "klyrow_provider_credential_key" in override
    assert "KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED=false" in example
