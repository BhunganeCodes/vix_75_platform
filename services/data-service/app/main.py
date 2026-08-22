"""data-service entry point: FastAPI app + background MT5 ingestion."""

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

from .db import Database
from .ingest import Ingestor
from .mt5_client import BridgeMT5Client
from .routes import router

if sys.platform == "win32":  # pragma: no cover - platform guard
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="data-service")
    configure_logging(
        settings.service_name,
        level=settings.log_level,
        json_output=settings.log_json,
    )

    database = Database(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        await database.connect()
        await redis_client.ping()
        logger.info("data-service dependencies ready")
    except Exception:
        # Stay up in degraded mode so /health reports the truth.
        logger.exception("dependency startup failed")

    ingestor = Ingestor(
        settings=settings,
        db=database,
        redis=redis_client,
        client=BridgeMT5Client(settings),
    )
    task = asyncio.create_task(ingestor.run_forever())

    app.state.settings = settings
    app.state.db = database
    app.state.redis = redis_client
    app.state.ingestor = ingestor

    yield

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await database.close()
    await redis_client.aclose()
    logger.info("data-service stopped")


app = FastAPI(title="vix75 data-service", version="0.2.0", lifespan=lifespan)
app.include_router(router)
