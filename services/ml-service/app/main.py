"""ml-service entry point: FastAPI app + inference worker."""

from __future__ import annotations

import asyncio
import contextlib

# psycopg3 async requires SelectorLoop on Windows (Proactor is default).
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

from .db import FeatureDatabaseML
from .routes import router
from .worker import InferenceWorker

if sys.platform == "win32":  # pragma: no cover - platform guard
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="ml-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    database = FeatureDatabaseML(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        await redis_client.ping()
        logger.info("ml-service dependencies ready")
    except Exception:
        logger.exception("dependency startup failed")

    worker = InferenceWorker(settings, database, redis_client)
    task = asyncio.create_task(worker.run_forever())

    app.state.settings = settings
    app.state.db = database
    app.state.redis = redis_client
    app.state.worker = worker

    yield

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await redis_client.aclose()
    logger.info("ml-service stopped")


app = FastAPI(title="vix75 ml-service", version="0.2.0", lifespan=lifespan)
app.include_router(router)
