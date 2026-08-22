"""Prometheus instrumentation for every FastAPI service.

Why not ``prometheus-fastapi-instrumentator``: as of FastAPI 0.141 the
router wrapper type (``fastapi.routing._IncludedRouter``) no longer
exposes ``.path``, which crashes the instrumentator's route-name
introspection middleware on ANY ``include_router`` call. This module
provides the same outcome with zero compatibility surface:

* ``http_requests_total{service,method,endpoint,status}`` counter
* ``http_request_duration_seconds`` histogram (p50/p99 friendly)
* ``GET /metrics`` exposition via ``prometheus_client.make_asgi_app``
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

from vix_core.logging import get_logger

ASGIApp = Callable[[Request], Awaitable[Response]]

logger = get_logger(__name__)

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["service", "method", "endpoint", "status"],
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["service", "method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def _endpoint_template(request: Request) -> str:
    """Low-cardinality route template ('/bars/{timeframe}'), best-effort."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else request.url.path


def attach_metrics(app: FastAPI, service_name: str) -> None:
    """Attach request metrics middleware + /metrics to an existing app."""

    @app.middleware("http")
    async def prometheus_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            status = "500"
            duration = time.perf_counter() - start
            endpoint = _endpoint_template(request)
            REQUEST_COUNT.labels(service_name, request.method, endpoint, status).inc()
            REQUEST_LATENCY.labels(service_name, request.method, endpoint).observe(duration)
            raise
        duration = time.perf_counter() - start
        endpoint = _endpoint_template(request)
        REQUEST_COUNT.labels(service_name, request.method, endpoint, status).inc()
        REQUEST_LATENCY.labels(service_name, request.method, endpoint).observe(duration)
        return response

    async def metrics(request: Request) -> Response:  # pragma: no cover - trivial
        del request
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    app.add_route("/metrics", metrics, methods=["GET"], include_in_schema=False)
    logger.debug("metrics attached", service=service_name)
