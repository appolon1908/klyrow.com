"""Bounded OTLP tracing with explicit, content-free attributes.

No global HTTP/database auto-instrumentation: URLs, SQL parameters, exception
messages and customer headers must not leak into telemetry by default.
"""
from contextlib import contextmanager
import os
import json
from urllib.parse import urlsplit

from opentelemetry import context, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_provider = None
_propagator = TraceContextTextMapPropagator()


def configure_tracing(service_name="klyrow-api"):
    global _provider
    if _provider is not None:
        return _provider
    endpoint = os.getenv("KLYROW_OTLP_TRACES_ENDPOINT", "")
    if not endpoint:
        return None
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid_otlp_endpoint")
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        ratio = float(os.getenv("KLYROW_TRACE_SAMPLE_RATIO", "0.1"))
        if not 0 <= ratio <= 1:
            raise ValueError("invalid_sample_ratio")
        provider = TracerProvider(resource=Resource({
            "service.name": service_name, "service.namespace": "codestra",
            "service.version": os.getenv("KLYROW_SOURCE_SHA", "unknown"),
            "deployment.environment": os.getenv("KLYROW_ENV", "development"),
        }), sampler=ParentBased(TraceIdRatioBased(ratio)))
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, timeout=2),
            max_queue_size=1024, max_export_batch_size=128,
            schedule_delay_millis=1000, export_timeout_millis=2500,
        ))
        _provider = provider
        return provider
    except Exception:
        # Export setup failure cannot take down mail. No exception text/config
        # is printed: it may include endpoint credentials from misconfiguration.
        return None


def trace_carrier():
    carrier = {}
    try:
        _propagator.inject(carrier)
    except Exception:
        pass
    return carrier


def stored_carrier(value):
    try:
        data = json.loads(value) if isinstance(value, str) else value
        return {k: v for k, v in data.items()
                if k in {"traceparent", "tracestate"} and isinstance(v, str) and len(v) <= 512}
    except Exception:
        return {}


@contextmanager
def traced(name, carrier=None, kind=trace.SpanKind.INTERNAL):
    span = None
    token = None
    try:
        parent = _propagator.extract(carrier) if carrier else context.get_current()
        tracer = (_provider or trace.get_tracer_provider()).get_tracer("codestra.klyrow")
        span = tracer.start_span(name, context=parent, kind=kind)
        token = context.attach(trace.set_span_in_context(span, parent))
    except Exception:
        pass
    try:
        yield span
    finally:
        if token is not None:
            try:
                context.detach(token)
            except Exception:
                pass
        if span is not None:
            try:
                span.end()
            except Exception:
                pass


def attributes(span, values):
    if span is not None:
        try:
            span.set_attributes(values)
        except Exception:
            pass


class TraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        carrier = {k.decode("ascii"): v.decode("ascii", errors="ignore")
                   for k, v in scope.get("headers", [])
                   if k in {b"traceparent", b"tracestate"} and len(v) <= 512}
        with traced("HTTP request", carrier, trace.SpanKind.SERVER) as span:
            async def capture(message):
                if message["type"] == "http.response.start":
                    attributes(span, {"http.response.status_code": message["status"]})
                await send(message)
            try:
                await self.app(scope, receive, capture)
            finally:
                route = scope.get("route")
                # Only the registered route template, never a requested URL.
                attributes(span, {"http.route": getattr(route, "path", "unmatched")})
