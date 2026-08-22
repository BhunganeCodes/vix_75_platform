"""feature-service: consumes bar events, computes features, publishes snapshots."""

from __future__ import annotations

import asyncio
import contextlib

# psycopg3 async requires SelectorLoop on Windows (Proactor is default).
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger
from vix_core.observability import attach_metrics

from .consumer import FeatureConsumer
from .db import FeatureDatabase

if sys.platform == "win32":  # pragma: no cover - platform guard
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="feature-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    database = FeatureDatabase(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        await database.connect()
        await redis_client.ping()
        logger.info("feature-service dependencies ready")
    except Exception:
        logger.exception("dependency startup failed")

    consumer = FeatureConsumer(settings, database, redis_client)
    task = asyncio.create_task(consumer.run_forever())

    app.state.settings = settings
    app.state.db = database
    app.state.redis = redis_client
    app.state.consumer = consumer

    yield

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await database.close()
    await redis_client.aclose()
    logger.info("feature-service stopped")


app = FastAPI(title="vix75 feature-service", version="0.2.0", lifespan=lifespan)
attach_metrics(app, "feature-service")


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    consumer = cast(FeatureConsumer, request.app.state.consumer)
    return {
        "service": "feature-service",
        "status": "ok",
        "events_processed": consumer.processed,
    }
