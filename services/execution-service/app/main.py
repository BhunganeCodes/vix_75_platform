"""execution-service entry point: FastAPI app + execution/reconciliation workers."""

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
from vix_core.observability import attach_metrics

if sys.platform == "win32":  # pragma: no cover - psycopg3 async requirement
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


from .consumer import ExecutionConsumer
from .db import ExecutionDatabase
from .mt5_executor import MT5Executor
from .reconciliation import Reconciler
from .routes import router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="execution-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )

    database = ExecutionDatabase(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    executor = MT5Executor(settings)

    try:
        await database.connect()
        await redis_client.ping()
        if settings.shadow_mode:
            logger.warning("SHADOW mode - live order_send disabled")
        else:
            await asyncio.to_thread(executor.client.connect)  # type: ignore[union-attr]
        logger.info("execution-service dependencies ready")
    except Exception:
        logger.exception("dependency startup failed")

    consumer = ExecutionConsumer(settings, database, redis_client, executor=executor)
    reconciler = Reconciler(settings, database, redis_client, mt5=executor.mt5)

    consumer_task = asyncio.create_task(consumer.run_forever())
    reconcile_task = asyncio.create_task(reconciler.run_forever())

    app.state.settings = settings
    app.state.db = database
    app.state.redis = redis_client
    app.state.executor = executor

    yield

    for task in (consumer_task, reconcile_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await database.close()
    await redis_client.aclose()
    logger.info("execution-service stopped")


app = FastAPI(title="vix75 execution-service", version="0.2.0", lifespan=lifespan)
attach_metrics(app, "execution-service")
app.include_router(router)
