import asyncio
import os
from pathlib import Path

from sqlalchemy import func, select

from apps.gateway.app.main import DB, Base, Tenant, engine
from apps.gateway.app import postal_provisioning as provisioning
from apps.gateway.app.postal_provisioning import PostalProvisioningOutbox, PostalTenantMapping


def setup_function():
    os.environ["KLYROW_ENV"] = "development"
    os.environ["KLYROW_ALLOW_LEGACY_GLOBAL_POSTAL_KEY"] = "false"
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as s:
        s.add(Tenant(id="tenant-provision", name="Provision Me", quota=1500))
        s.commit()


def test_enqueue_is_idempotent():
    with DB() as s:
        first = provisioning.enqueue_postal_provisioning(s, "tenant-provision")
        second = provisioning.enqueue_postal_provisioning(s, "tenant-provision")
        assert first.id == second.id
        assert s.scalar(select(func.count()).select_from(PostalProvisioningOutbox)) == 1


def test_provisioning_stores_encrypted_tenant_credential(monkeypatch):
    async def fake_bridge(_tenant):
        return {
            "organization_id": "org-1",
            "organization_permalink": "org-one",
            "server_id": "server-1",
            "server_permalink": "server-one",
            "mode": "Development",
            "api_key": "provider-api-key-fixture-value-123456",
        }

    monkeypatch.setattr(provisioning, "_call_bridge", fake_bridge)
    with DB() as s:
        provisioning.enqueue_postal_provisioning(s, "tenant-provision")
    assert asyncio.run(provisioning.provisioning_tick()) == 1
    with DB() as s:
        mapping = s.get(PostalTenantMapping, "tenant-provision")
        assert mapping.state == "READY" and mapping.provider_mode == "Development"
        assert "provider-api-key-fixture" not in mapping.api_key_ciphertext
        assert mapping.api_key_fingerprint.startswith("sha256:")
        assert (
            provisioning.tenant_postal_api_key(s, "tenant-provision")
            == "provider-api-key-fixture-value-123456"
        )


def test_unprovisioned_tenant_cannot_use_global_postal_credential():
    with DB() as s:
        try:
            provisioning.tenant_postal_api_key(s, "tenant-provision")
        except RuntimeError as exc:
            assert "not provisioned" in str(exc)
        else:
            raise AssertionError("unprovisioned tenant inherited a global provider credential")


def test_retryable_failure_is_durable(monkeypatch):
    async def failed_bridge(_tenant):
        raise OSError("bridge unavailable")

    monkeypatch.setattr(provisioning, "_call_bridge", failed_bridge)
    with DB() as s:
        provisioning.enqueue_postal_provisioning(s, "tenant-provision")
    assert asyncio.run(provisioning.provisioning_tick()) == 0
    with DB() as s:
        mapping = s.get(PostalTenantMapping, "tenant-provision")
        job = s.scalar(
            select(PostalProvisioningOutbox).where(
                PostalProvisioningOutbox.tenant_id == "tenant-provision"
            )
        )
        assert (
            mapping.state == "RETRYABLE_FAILURE"
            and job.state == "RETRYABLE_FAILURE"
            and job.attempts == 1
        )


def test_postal_bridge_uses_models_and_development_mode_not_direct_sql():
    source = (Path(__file__).parents[1] / "apps/postal-provisioner/provisioner.rb").read_text(
        encoding="utf-8"
    )
    assert 'row.mode = "Development"' in source
    assert "Organization.find_or_create_by!" in source
    assert "server.credentials.find_or_create_by!" in source
    assert "UPDATE " not in source.upper() and "INSERT INTO" not in source.upper()
