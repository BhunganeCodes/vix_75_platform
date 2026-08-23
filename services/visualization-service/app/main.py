"""visualization-service: Plotly chart rendering for the VIX75 platform."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

if sys.platform == "win32":  # pragma: no cover
    asyncio_set_policy = True
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import redis.asyncio as aioredis
from fastapi import FastAPI
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger
from vix_core.observability import attach_metrics

from .db import VizDatabase
from .routes import router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="visualization-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    database = VizDatabase(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await database.connect()
        logger.info("visualization-service dependencies ready")
    except Exception:
        logger.exception("dependency startup failed")

    app.state.settings = settings
    app.state.db = database
    app.state.redis = redis_client

    yield

    await database.close()
    await redis_client.aclose()
    logger.info("visualization-service stopped")


app = FastAPI(title="vix75 visualization-service", version="0.1.0", lifespan=lifespan)
app.include_router(router)
attach_metrics(app, "visualization-service")
