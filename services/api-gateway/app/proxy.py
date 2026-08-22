"""Catch-all reverse proxy to internal services (never host-exposed).

Only the gateway is reachable from outside; everything else lives on the
internal Docker network. Each proxied request:

* resolves the service alias to an internal URL,
* injects/propagates ``X-Correlation-Id``,
* forwards method, query params, body and safe headers via a shared
  injected ``httpx.AsyncClient`` (fully async - no event-loop blocking).
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from starlette.background import BackgroundTask
from vix_core.correlation import CORRELATION_HEADER
from vix_core.logging import get_logger

logger = get_logger(__name__)

# Docker-network aliases; the gateway and services share the compose network
# where every container listens on 8000 regardless of its HOST port map.
SERVICE_ROUTES: dict[str, str] = {
    "signals": "http://signal-service:8000",
    "signal-service": "http://signal-service:8000",
    "risk": "http://risk-service:8000",
    "risk-service": "http://risk-service:8000",
    "ml": "http://ml-service:8000",
    "ml-service": "http://ml-service:8000",
    "data": "http://data-service:8000",
    "data-service": "http://data-service:8000",
    "features": "http://feature-service:8000",
    "feature-service": "http://feature-service:8000",
    "execution": "http://execution-service:8000",
    "execution-service": "http://execution-service:8000",
    "notify": "http://notify-service:8000",
    "notify-service": "http://notify-service:8000",
}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

PROXY_TIMEOUT_S = 15.0


def _safe_headers(headers: Any) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def build_proxy_router(
    client: httpx.AsyncClient,
    *,
    routes: dict[str, str] | None = None,
) -> APIRouter:
    """Factory so tests can inject a MockTransport-backed client."""
    router = APIRouter(tags=["proxy"])
    route_map = routes or SERVICE_ROUTES

    @router.api_route(
        "/{service}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def proxy(service: str, path: str, request: Request) -> Response:
        base = route_map.get(service)
        if base is None:
            return Response(
                content=f'{{"detail":"unknown service {service}"}}',
                status_code=404,
                media_type="application/json",
            )

        correlation_id = getattr(request.state, "correlation_id", None) or (
            request.headers.get(CORRELATION_HEADER) or ""
        )
        forward_headers = _safe_headers(request.headers)
        # Drop any inbound correlation header (any casing) so the single
        # canonical entry below is the only one that reaches upstream.
        for name in [k for k in forward_headers if k.lower() == CORRELATION_HEADER.lower()]:
            del forward_headers[name]
        forward_headers[CORRELATION_HEADER] = correlation_id

        target = f"{base}/{path}"
        logger.info(
            "proxying",
            service=service,
            path=path,
            method=request.method,
            target=target,
        )
        try:
            upstream = await client.request(
                request.method,
                target,
                params=dict(request.query_params),
                content=await request.body(),
                headers=forward_headers,
                timeout=PROXY_TIMEOUT_S,
            )
        except httpx.TimeoutException:
            logger.exception("upstream timeout", service=service, path=path)
            return Response(
                content='{"detail":"upstream timeout"}',
                status_code=504,
                media_type="application/json",
                headers={CORRELATION_HEADER: correlation_id},
            )
        except httpx.HTTPError:
            logger.exception("upstream unreachable", service=service)
            return Response(
                content='{"detail":"upstream unavailable"}',
                status_code=502,
                media_type="application/json",
                headers={CORRELATION_HEADER: correlation_id},
            )

        response_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
        }
        response_headers[CORRELATION_HEADER] = correlation_id
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(upstream.aclose),
        )

    return router
