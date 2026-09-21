from pathlib import Path

from fastapi.testclient import TestClient

from apps.gateway.app.platform import app


client = TestClient(app, base_url="https://app.klyrow.test")


def test_m6c_document_routes_are_registered_before_spa_fallback():
    paths = {getattr(route, "path", "") for route in app.routes}
    assert {
        "/app/api/billing/invoices/{invoice_id}/document",
        "/app/api/billing/credit-notes",
        "/app/api/billing/credit-notes/{credit_note_id}/document",
        "/app/api/billing/payments/{payment_id}/receipt",
    } <= paths
    for path in (
        "/app/api/billing/invoices/invoice-1/document",
        "/app/api/billing/credit-notes",
        "/app/api/billing/payments/payment-1/receipt",
    ):
        response = client.get(path)
        assert response.status_code == 401, (path, response.status_code, response.text)
        assert response.headers.get("content-type", "").startswith("application/json")
