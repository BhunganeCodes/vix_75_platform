"""One-shot consumer runner: `python -m app.consumer_once [--block-ms N]`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

if sys.platform == "win32":  # pragma: no cover - psycopg3 async requirement
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import redis.asyncio as aioredis
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

from .consumer import ExecutionConsumer
from .db import ExecutionDatabase
from .mt5_executor import MT5Executor

logger = get_logger(__name__)


async def _run(block_ms: int) -> int:
    settings = Settings(service_name="execution-consumer-once")
    configure_logging(settings.service_name, level=settings.log_level)

    db = ExecutionDatabase(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await db.connect()
        await redis_client.ping()
        consumer = ExecutionConsumer(
            settings,
            db,
            redis_client,
            executor=MT5Executor(
                settings, backoff_base_s=0.01
            ),  # fast backoff for one-shot/test contexts
        )
        return await consumer.run_once(block_ms=block_ms)
    finally:
        await db.close()
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
