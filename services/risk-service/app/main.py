"""risk-service entry point: FastAPI app + gating consumer."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

if sys.platform == "win32":  # pragma: no cover - platform guard
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from .consumer import RiskConsumer
from .exposure import OPEN_KEY, ExposureTracker

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="risk-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("risk-service dependencies ready")
    except Exception:
        logger.exception("dependency startup failed")

    consumer = RiskConsumer(settings, redis_client)
    task = asyncio.create_task(consumer.run_forever())

    app.state.settings = settings
    app.state.redis = redis_client
    app.state.consumer = consumer

    yield

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await redis_client.aclose()
    logger.info("risk-service stopped")


app = FastAPI(title="vix75 risk-service", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health(request: object) -> dict[str, object]:
    state = getattr(request, "app", None)
    consumer = getattr(getattr(state, "state", None), "consumer", None)
    tracker: ExposureTracker | None = getattr(consumer, "_tracker", None)
    return {
        "service": "risk-service",
        "status": "ok",
        "open_positions": tracker.open_count() if tracker else None,
        "total_open_risk": round(tracker.total_open_risk(), 2) if tracker else None,
        "stream_key": OPEN_KEY,
    }
