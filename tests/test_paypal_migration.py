from pathlib import Path


def test_paypal_active_checkout_migration_prevents_cross_provider_double_checkout():
    migration = Path("migrations/2026092103_paypal_provider.sql").read_text(encoding="utf-8")
    assert "uq_klyrow_active_hosted_checkout_per_invoice" in migration
    assert "provider IN ('stripe', 'paypal')" in migration
    assert "CREATED" in migration and "AUTHORIZED" in migration
