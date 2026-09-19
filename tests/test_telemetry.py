from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apps.gateway.app import telemetry


def test_trace_propagates_across_durable_handoff_without_customer_data(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_provider", provider)
    app = FastAPI()
    app.add_middleware(telemetry.TraceMiddleware)
    carriers = []
    @app.get("/messages/{message_id}")
    def endpoint(message_id: str):
        carriers.append(telemetry.trace_carrier())
        return {"ok": True}
    incoming = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    with TestClient(app) as client:
        assert client.get("/messages/customer@example.com?secret=hidden", headers={"traceparent": incoming}).status_code == 200
    with telemetry.traced("postal submit", carriers[0]):
        assert telemetry.trace_carrier()["traceparent"].split("-")[1] == incoming.split("-")[1]
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[0].attributes["http.route"] == "/messages/{message_id}"
    assert "customer@example.com" not in repr(spans[0].attributes)
    assert "hidden" not in repr(spans[0].attributes)
    assert spans[1].parent.span_id == spans[0].context.span_id
    provider.shutdown()


def test_broken_exporter_cannot_fail_application_or_hide_its_errors(monkeypatch):
    class Broken:
        def get_tracer(self, *args):
            raise RuntimeError("exporter unavailable")
    monkeypatch.setattr(telemetry, "_provider", Broken())
    with telemetry.traced("accept"):
        value = 42
    assert value == 42
    import pytest
    with pytest.raises(ValueError, match="business failure"):
        with telemetry.traced("accept"):
            raise ValueError("business failure")
    assert telemetry.stored_carrier("<!DOCTYPE html>") == {}
