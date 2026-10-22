"""FastAPI app factory: OTel tracing, JSON logs, and the health/metrics endpoints."""

import logging
import os
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Response
from opentelemetry import propagate, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from demo_common.logs import setup_logging

ReadyCheck = Callable[[], Awaitable[bool] | bool]


class SampledOnlyTraceContext(TraceContextTextMapPropagator):
    """W3C trace context that sends only the sampled flag. The SDK also sets the newer
    random-trace-id flag (traceparent ...-03), and APISIX's opentelemetry-lua rejects any
    flags above 02 and starts a new trace, so a request the gateway blocks lost its link
    to the caller's trace (D-061)."""

    def inject(self, carrier, context=None, setter=None):
        span_context = trace.get_current_span(context).get_span_context()
        if span_context.is_valid and span_context.trace_flags & ~trace.TraceFlags.SAMPLED:
            context = trace.set_span_in_context(trace.NonRecordingSpan(trace.SpanContext(
                span_context.trace_id, span_context.span_id, span_context.is_remote,
                trace.TraceFlags(span_context.trace_flags & trace.TraceFlags.SAMPLED),
                span_context.trace_state)), context)
        if setter is None:
            super().inject(carrier, context)
        else:
            super().inject(carrier, context, setter)


def setup_telemetry(service: str) -> None:
    """W3C trace context is the default propagator. Spans and logs are exported over OTLP
    only when OTEL_EXPORTER_OTLP_ENDPOINT is set, so services also run without a collector.
    Logs still go to stdout as JSON either way (OBS-04)."""
    resource = Resource.create({"service.name": service})
    provider = TracerProvider(resource=resource)
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # Short delay keeps dashboards inside the 15 s freshness bound (OBS-06)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=1000)
        )
        set_logger_provider(log_provider)
        logging.getLogger().addHandler(LoggingHandler(logger_provider=log_provider))
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(SampledOnlyTraceContext())
    HTTPXClientInstrumentor().instrument()


def create_app(service: str, ready_check: ReadyCheck | None = None, lifespan=None) -> FastAPI:
    setup_logging(service, os.environ.get("LOG_LEVEL", "INFO"))
    setup_telemetry(service)
    app = FastAPI(title=service, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(response: Response) -> dict:
        ready = True
        if ready_check is not None:
            result = ready_check()
            ready = await result if isinstance(result, Awaitable) else result
        if not ready:
            response.status_code = 503
        return {"ready": ready}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    return app


def run(app: FastAPI, port: int | None = None) -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port or int(os.environ.get("PORT", "8000")),
        log_config=None,
        access_log=False,
    )
