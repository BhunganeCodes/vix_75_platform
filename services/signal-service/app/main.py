"""signal-service entry point: FastAPI app + confluence consumer."""

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

if sys.platform == "win32":  # pragma: no cover - psycopg3 async requirement
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from .consumer import SignalConsumer
from .db import SignalDatabase
from .routes import router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="signal-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    database = SignalDatabase(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        await database.connect()
        await redis_client.ping()
        logger.info("signal-service dependencies ready")
    except Exception:
        logger.exception("dependency startup failed")

    consumer = SignalConsumer(settings, database, redis_client)
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
    logger.info("signal-service stopped")


app = FastAPI(title="vix75 signal-service", version="0.2.0", lifespan=lifespan)
app.include_router(router)
