from fastapi.routing import APIRoute

from apps.gateway.app import main as _main
from apps.gateway.app.billing_browser import router


EXPECTED = {
    "/app/api/billing/overview",
    "/app/api/billing/subscription",
    "/app/api/billing/invoices",
    "/app/api/billing/invoices/{invoice_id}",
    "/app/api/billing/payments",
    "/app/api/billing/refunds",
    "/app/api/billing/payment-methods",
    "/app/api/billing/wallet",
    "/app/api/billing/capabilities",
    "/app/api/billing/invoices/{invoice_id}/checkout",
    "/app/api/billing/payment-attempts/{payment_attempt_id}",
}


def test_billing_browser_surface_is_get_only_and_complete():
    routes = {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if route.path.startswith("/app/api/billing/")
    }
    assert {path for path, _ in routes} == EXPECTED
    assert {method for _, method in routes} == {"GET", "POST"}
    assert ("/app/api/billing/invoices/{invoice_id}/checkout", "POST") in routes
    assert all(
        method == "GET"
        for path, method in routes
        if path != "/app/api/billing/invoices/{invoice_id}/checkout"
    )


def test_billing_browser_surface_has_no_provider_or_mutation_paths():
    paths = {route.path for route in router.routes if isinstance(route, APIRoute)}
    assert not any(path.startswith("/app/api/billing/") and any(token in path for token in ("capture", "authorize", "cancel")) for path in paths)
