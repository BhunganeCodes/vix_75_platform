"""api-gateway entry point: JWT auth, rate limiting, reverse proxy, metrics."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from vix_core.config import Settings
from vix_core.correlation import CORRELATION_HEADER, new_correlation_id
from vix_core.logging import configure_logging, get_logger
from vix_core.observability import attach_metrics

if sys.platform == "win32":  # pragma: no cover - platform guard
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from .auth import build_auth_router, verify_token
from .proxy import SERVICE_ROUTES, build_proxy_router
from .rate_limiter import SlidingWindowLimiter

logger = get_logger(__name__)


def _correlation_middleware(app: FastAPI) -> None:
    """Stamp every request/response with a correlation id + bind logging."""

    @app.middleware("http")
    async def correlation_header(request: Request, call_next: Any) -> Any:
        incoming = request.headers.get(CORRELATION_HEADER)
        correlation_id = incoming or new_correlation_id()
        request.state.correlation_id = correlation_id

        from vix_core.correlation import bind_correlation_id, unbind_correlation_id

        bind_correlation_id(correlation_id)
        try:
            response = await call_next(request)
        finally:
            unbind_correlation_id()
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(
        settings.service_name or "api-gateway",
        level=settings.log_level,
        json_output=settings.log_json,
    )
    logger.info(
        "gateway ready",
        rate_limit_per_minute=settings.rate_limit_per_minute,
        services=sorted({s.rsplit("-", 1)[0] for s in SERVICE_ROUTES if "-" not in s}),
    )
    yield
    logger.info("gateway stopped")


def build_app(
    *,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    redis_client: aioredis.Redis | None = None,
) -> FastAPI:
    """Factory so tests can inject a MockTransport client + fake Redis."""
    resolved_settings = settings or Settings(service_name="api-gateway")

    app = FastAPI(title="vix75 api-gateway", version="0.2.0")
    app.state.settings = resolved_settings

    _correlation_middleware(app)

    limiter = SlidingWindowLimiter(
        redis_client,
        limit_per_window=resolved_settings.rate_limit_per_minute,
        window_seconds=resolved_settings.rate_limit_window_seconds,
    )

    # ---- Public routes (no auth) ---------------------------------------

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "service": "api-gateway",
            "status": "ok",
            "redis": "up" if redis_client is not None else "down",
            "rate_limit_per_minute": resolved_settings.rate_limit_per_minute,
        }

    app.include_router(build_auth_router(resolved_settings))

    # ---- Protected proxy -------------------------------------------------

    async def enforce(
        request: Request, claims: Annotated[dict[str, Any], Depends(verify_token)]
    ) -> None:
        subject = str(claims.get("sub", "anonymous"))
        decision = await limiter.check(subject)
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={
                    "Retry-After": str(decision.retry_after_s),
                    CORRELATION_HEADER: getattr(
                        request.state, "correlation_id", new_correlation_id()
                    ),
                },
            )

    proxy_router = build_proxy_router(http_client or httpx.AsyncClient(timeout=20.0))
    app.include_router(proxy_router, dependencies=[Depends(enforce)])

    # ---- Metrics (internal; instrumentator exposes /metrics) --------------
    attach_metrics(app, "api-gateway")

    # Correlation ids ride on error responses too.
    @app.exception_handler(HTTPException)
    async def http_exception_with_correlation(request: Request, exc: HTTPException) -> JSONResponse:
        headers = dict(exc.headers or {})
        headers.setdefault(
            CORRELATION_HEADER,
            getattr(request.state, "correlation_id", None) or new_correlation_id(),
        )
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}, headers=headers
        )

    return app


app = build_app()
