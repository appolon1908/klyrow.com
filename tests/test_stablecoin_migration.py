from pathlib import Path


def test_stablecoin_migration_is_tenant_scoped_and_extends_active_checkout_uniqueness():
    migration = Path("migrations/2026092104_stablecoin_usdc_authority.sql").read_text(encoding="utf-8")
    assert "klyrow_stablecoin_payment_requests" in migration
    assert "klyrow_stablecoin_chain_events" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "current_setting(''app.tenant_id'', true)" in migration
    assert "provider IN ('stripe', 'paypal', 'stablecoin')" in migration
    assert "uq_klyrow_stablecoin_chain_tx" in migration
