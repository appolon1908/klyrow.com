from pathlib import Path

from apps.gateway.app import main


def test_tenant_rls_binding_is_sqlite_safe():
    main.Base.metadata.create_all(main.engine)
    with main.DB() as session:
        main.bind_tenant_rls(session, "tenant-a")


def test_authenticated_api_and_browser_context_bind_request_tenant_before_routes():
    main_source = Path("apps/gateway/app/main.py").read_text(encoding="utf-8")
    browser_source = Path("apps/gateway/app/auth_bff.py").read_text(encoding="utf-8")

    assert 'func.set_config("app.tenant_id", tenant_id, True)' in main_source
    assert 'bind_tenant_rls(s, ctx["tenant"])' in main_source
    assert "bind_tenant_rls(s, session.tenant_id)" in browser_source
