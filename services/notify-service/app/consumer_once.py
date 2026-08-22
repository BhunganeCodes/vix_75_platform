"""One-shot consumer runner for notify-service."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

if sys.platform == "win32":  # pragma: no cover - psycopg3 async requirement
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import redis.asyncio as aioredis
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

from .consumer import NotifyConsumer
from .lifecycle import LifecycleLogger
from .telegram import TelegramSender

logger = get_logger(__name__)


async def _run(block_ms: int) -> int:
    settings = Settings(service_name="notify-consumer-once")
    configure_logging(settings.service_name, level=settings.log_level)

    lifecycle = LifecycleLogger(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    sender = TelegramSender(
        settings, httpx.AsyncClient(base_url="https://api.telegram.org", timeout=10.0)
    )
    try:
        await lifecycle.connect()
        consumer = NotifyConsumer(settings, redis_client, lifecycle, sender)
        return await consumer.run_once(block_ms=block_ms)
    finally:
        await lifecycle.close()
        with contextlib.suppress(Exception):
            await redis_client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block-ms", type=int, default=1_500)
    args = parser.parse_args()
    processed = asyncio.run(_run(args.block_ms))
    logger.info("drained", events=processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
