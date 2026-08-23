"""notify-service entry point: FastAPI app + lifecycle/alert consumer."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger
from vix_core.observability import attach_metrics

if sys.platform == "win32":  # pragma: no cover - psycopg3 async requirement
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


from .consumer import NotifyConsumer
from .lifecycle import LifecycleLogger
from .telegram import TelegramSender

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="notify-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )

    lifecycle = LifecycleLogger(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    sender = TelegramSender(
        settings, httpx.AsyncClient(base_url="https://api.telegram.org", timeout=10.0)
    )

    try:
        await lifecycle.connect()
        await redis_client.ping()
        logger.info(
            "notify-service dependencies ready",
            telegram_configured=sender.configured,
            alert_rejections=settings.alert_rejections,
        )
    except Exception:
        logger.exception("dependency startup failed")

    consumer = NotifyConsumer(settings, redis_client, lifecycle, sender)
    task = asyncio.create_task(consumer.run_forever())

    app.state.settings = settings
    app.state.lifecycle = lifecycle
    app.state.consumer = consumer

    yield

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await lifecycle.close()
    await redis_client.aclose()
    logger.info("notify-service stopped")


app = FastAPI(title="vix75 notify-service", version="0.2.0", lifespan=lifespan)
attach_metrics(app, "notify-service")


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    from typing import cast

    from .consumer import NotifyConsumer

    consumer = cast(NotifyConsumer, request.app.state.consumer)
    return {
        "service": "notify-service",
        "status": "ok",
        "events_processed": consumer.processed,
        "alerts_sent": consumer.alerts_sent,
    }
