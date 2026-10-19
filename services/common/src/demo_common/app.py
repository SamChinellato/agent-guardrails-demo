"""FastAPI app factory: OTel tracing, JSON logs, and the health/metrics endpoints."""

import os
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from demo_common.logs import setup_logging

ReadyCheck = Callable[[], Awaitable[bool] | bool]


def setup_tracing(service: str) -> None:
    """W3C trace context is the default propagator. Spans are exported only when
    OTEL_EXPORTER_OTLP_ENDPOINT is set, so services run without a collector too."""
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()


def create_app(service: str, ready_check: ReadyCheck | None = None) -> FastAPI:
    setup_logging(service, os.environ.get("LOG_LEVEL", "INFO"))
    setup_tracing(service)
    app = FastAPI(title=service, docs_url=None, redoc_url=None, openapi_url=None)

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
