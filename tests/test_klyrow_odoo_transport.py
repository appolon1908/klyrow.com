"""Exercise the owned Odoo transport with HTTPX, without a live Middleware app.

Only settings and the database session are substituted. Requests go exclusively
through MockTransport; no provider, Odoo database, or email is contacted.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "integrations/codestra-middleware/app/workers/klyrow_mail_odoo.py"
)


@pytest.fixture
def worker(monkeypatch, tmp_path):
    config = ModuleType("app.core.config")
    config.settings = SimpleNamespace(
        klyrow_mail_odoo_api_key_file=str(tmp_path / "test-key"),
        klyrow_mail_odoo_url="https://odoo.example.test",
        klyrow_mail_odoo_database="test",
        klyrow_mail_odoo_username="test",
        klyrow_mail_worker_max_attempts=3,
    )
    Path(config.settings.klyrow_mail_odoo_api_key_file).write_text("synthetic-unit-test-only")
    monkeypatch.setitem(sys.modules, "app.core.config", config)
    spec = importlib.util.spec_from_file_location("_klyrow_odoo_transport_test", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def rpc(result, ident="idem-test"):
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(handler))
    )


@pytest.mark.parametrize("phase", ["auth", "ingest"])
@pytest.mark.parametrize(
    "defect", ["wrong_id", "missing_id", "wrong_version", "both_result_and_error"]
)
def test_rpc_envelope_must_bind_the_exact_request(worker, monkeypatch, phase, defect):
    calls = []

    def handler(request):
        document = json.loads(request.content)
        calls.append(document)
        response = rpc(7 if document["params"]["service"] == "common" else 42, document["id"])
        if (phase == "auth") == (len(calls) == 1):
            if defect == "wrong_id":
                response["id"] = "different-request"
            elif defect == "missing_id":
                response.pop("id")
            elif defect == "wrong_version":
                response["jsonrpc"] = "1.0"
            else:
                response["error"] = None
        return httpx.Response(200, json=response)

    install_transport(monkeypatch, handler)
    with pytest.raises(worker.DeliveryFailure, match="invalid_odoo_response") as failure:
        asyncio.run(worker.RestrictedOdooTransport().deliver({}, "idem-test"))
    assert not failure.value.permanent
    assert len(calls) == (1 if phase == "auth" else 2)


@pytest.mark.parametrize("phase", ["auth", "ingest"])
@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.DecodingError]
)
def test_protocol_failures_enter_bounded_retry(worker, monkeypatch, phase, error):
    def handler(request):
        document = json.loads(request.content)
        if (phase == "auth") == document["id"].endswith(":auth"):
            raise error("private-provider-detail", request=request)
        return httpx.Response(200, json=rpc(7, document["id"]))

    install_transport(monkeypatch, handler)
    with pytest.raises(worker.DeliveryFailure) as failure:
        asyncio.run(worker.RestrictedOdooTransport().deliver({}, "idem-test"))
    assert str(failure.value) == "odoo_unavailable"
    assert not failure.value.permanent


@pytest.mark.parametrize("result", [42, {"id": 42}])
def test_valid_correlated_acknowledgment(worker, monkeypatch, result):
    calls = []

    def handler(request):
        document = json.loads(request.content)
        calls.append(document)
        return httpx.Response(200, json=rpc(7 if len(calls) == 1 else result, document["id"]))

    install_transport(monkeypatch, handler)
    assert asyncio.run(worker.RestrictedOdooTransport().deliver({}, "idem-test")) == {
        "odoo_record_id": 42
    }
    assert calls[1]["params"]["args"][3:5] == ["codestra.mail.inbound.event", "ingest_event"]


@pytest.mark.parametrize("phase", ["auth", "ingest"])
@pytest.mark.parametrize("result", [True, False, 0, -1, "7", None])
def test_boolean_or_invalid_ids_never_acknowledge_delivery(worker, monkeypatch, phase, result):
    def handler(request):
        document = json.loads(request.content)
        value = result if (phase == "auth") == document["id"].endswith(":auth") else 7
        return httpx.Response(200, json=rpc(value, document["id"]))

    install_transport(monkeypatch, handler)
    code = "odoo_authentication_rejected" if phase == "auth" else "invalid_odoo_ack"
    with pytest.raises(worker.DeliveryFailure, match=code):
        asyncio.run(worker.RestrictedOdooTransport().deliver({}, "idem-test"))


@pytest.mark.parametrize("phase", ["auth", "ingest"])
def test_access_rejection_is_permanent_and_sanitized(worker, monkeypatch, phase):
    def handler(request):
        document = json.loads(request.content)
        if (phase == "auth") != document["id"].endswith(":auth"):
            return httpx.Response(200, json=rpc(7, document["id"]))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": document["id"],
                "error": {
                    "code": 100,
                    "message": "private-provider-detail",
                    "data": {"name": "odoo.exceptions.AccessError"},
                },
            },
        )

    install_transport(monkeypatch, handler)
    with pytest.raises(worker.DeliveryFailure) as failure:
        asyncio.run(worker.RestrictedOdooTransport().deliver({}, "idem-test"))
    assert str(failure.value) == "odoo_rejected"
    assert failure.value.permanent


@pytest.mark.parametrize("mode", ["malformed_json", "wrong_id", "protocol_error"])
def test_poison_response_reaches_dead_letter_and_releases_lease(worker, monkeypatch, mode):
    def handler(request):
        document = json.loads(request.content)
        if document["id"].endswith(":auth"):
            return httpx.Response(200, json=rpc(7, document["id"]))
        if mode == "protocol_error":
            raise httpx.RemoteProtocolError("private-provider-detail", request=request)
        if mode == "wrong_id":
            return httpx.Response(200, json=rpc(42, "other-command"))
        return httpx.Response(200, content="broken-json")

    install_transport(monkeypatch, handler)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(first=lambda: ("event-test",))
    row = {
        "payload": {"sender": "sender@example.com"},
        "event_id": "event-test",
        "provider_event_id": "provider-test",
        "idempotency_key": "idem-test",
        "inbound_id": "inbound-test",
        "recipient": "receiver@example.com",
        "lease_token": "lease-test",
    }
    for attempt, expected in enumerate(("retry_wait", "retry_wait", "dead_letter")):
        result = asyncio.run(
            worker.process(db, {**row, "attempts": attempt}, worker.RestrictedOdooTransport())
        )
        assert result == expected
        statement, params = db.execute.call_args.args
        assert "lease_token=:token" in str(statement)
        assert "lease_token=NULL" in str(statement)
        assert params["attempts"] == attempt + 1
        assert params["token"] == "lease-test"
        assert params["error"] == (
            "odoo_unavailable" if mode == "protocol_error" else "invalid_odoo_response"
        )
    assert db.commit.await_count == 3
