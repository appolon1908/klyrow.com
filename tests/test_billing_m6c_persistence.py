from pathlib import Path


def test_m6c_persistence_migration_is_tenant_isolated():
    migration = Path("migrations/2026092102_billing_m6c_persistence.sql").read_text(encoding="utf-8")
    for column in ("jurisdiction", "tax_rate", "tax_amount"):
        assert column in migration
    for table in ("klyrow_disputes", "klyrow_invoice_receipts"):
        assert table in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "current_setting(''app.tenant_id'', true)" in migration
    assert "uq_klyrow_invoice_receipt_payment" in migration
