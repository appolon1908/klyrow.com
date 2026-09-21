from pathlib import Path


def test_webmail_rls_migration_covers_all_browser_mail_tenant_tables():
    sql = Path("migrations/2026092105_webmail_rls.sql").read_text(encoding="utf-8")
    for table in (
        "webmail_mailboxes",
        "webmail_access",
        "webmail_messages",
        "webmail_attachments",
        "inbound_route_configs",
    ):
        assert f"'{table}'" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "current_setting(''app.tenant_id'', true)" in sql
    assert "WITH CHECK" in sql


def test_webmail_rls_uses_the_existing_least_privilege_runtime_role_contract():
    least_privilege = Path("migrations/2026090208_runtime_database_least_privilege.sql").read_text(encoding="utf-8")
    billing_rls = Path("migrations/2026092101_billing_rls_runtime_roles.sql").read_text(encoding="utf-8")
    assert "klyrow_runtime" in least_privilege
    assert "NOBYPASSRLS" in least_privilege or "NOBYPASSRLS" in billing_rls
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO klyrow_runtime" in least_privilege
