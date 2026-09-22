from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_browser_observability_is_read_only_and_governed():
    source = (ROOT / "apps/gateway/app/browser_observability.py").read_text()
    assert '@router.get("/app/api/admin/observability/webmail-postal")' in source
    assert "platform_admin_required" in source
    assert "Caddy -> Kong -> Middleware" in source
    for forbidden in ("httpx.", "requests.", "postal_url", "odoo_url", "prometheus_url"):
        assert forbidden not in source.lower()

def test_admin_observability_ui_registered():
    routes = (ROOT / "apps/web/src/portal/routes.ts").read_text()
    pages = (ROOT / "apps/web/src/portal/pages/index.ts").read_text()
    page = (ROOT / "apps/web/src/portal/pages/AdminObservabilityPage.vue").read_text()
    assert "/admin/observability" in routes
    assert "GET /app/api/admin/observability/webmail-postal" in routes
    assert "'admin-observability': AdminObservabilityPage" in pages
    assert "Caddy → Kong → Middleware" in page
