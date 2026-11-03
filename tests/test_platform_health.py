from fastapi.testclient import TestClient

from apps.gateway.app.main import app

client = TestClient(app)


def test_version_contract(monkeypatch):
    monkeypatch.setenv("KLYROW_RELEASE_VERSION", "2026.08.22")
    monkeypatch.setenv("KLYROW_RELEASE_SHA", "abc123")
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {
        "service": "klyrow-gateway",
        "version": "2026.08.22",
        "revision": "abc123",
    }
