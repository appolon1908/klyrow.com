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


def test_health_thresholds_are_server_authoritative():
    api = (ROOT / "apps/gateway/app/browser_observability.py").read_text()
    ui = (ROOT / "apps/web/src/portal/pages/AdminObservabilityPage.vue").read_text()
    assert '"thresholds"' in api
    assert '"health"' in api
    assert "oldest_seconds > 300" in api
    assert "page.data.value.health" in ui
    assert "page.data.value.thresholds" in ui


def test_observability_endpoint_inventory_and_incident_logic():
    api = (ROOT / "apps/gateway/app/browser_observability.py").read_text()
    routes = (ROOT / "apps/web/src/portal/routes.ts").read_text()
    for endpoint in (
        "/app/api/admin/observability/webmail-postal",
        "/app/api/admin/observability/webmail-postal/slo",
        "/app/api/admin/observability/webmail-postal/incidents",
        "/app/api/admin/observability/webmail-postal/architecture",
    ):
        assert endpoint in api
        assert endpoint in routes
    assert '"direct_cross_system_writes": False' in api
    assert "reconcile indeterminate work before retry" in api


def test_trace_drilldown_is_bounded_and_redacted():
    api = (ROOT / "apps/gateway/app/browser_observability.py").read_text()
    ui = (ROOT / "apps/web/src/portal/pages/AdminObservabilityPage.vue").read_text()
    assert '/app/api/admin/observability/webmail-postal/traces/{correlation_id}' in api
    assert "invalid_correlation_id" in api
    assert '"direct_cross_system_writes": False' in api
    assert "No addresses, subjects, bodies, tokens, provider IDs or payloads" in api
    assert "Trace explorer" in ui
    assert "Inspect trace" in ui
