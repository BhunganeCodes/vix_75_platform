"""One-shot consumer runner: `python -m app.consumer [--block-ms N]`.

Used by integration tests and ops scripts to drain currently-pending
stream messages without starting the HTTP server.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

if sys.platform == "win32":  # pragma: no cover - psycopg3 async requirement
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

from .consumer import SignalConsumer
from .db import SignalDatabase

logger = get_logger(__name__)


async def _run(block_ms: int) -> int:
    settings = Settings(service_name="signal-consumer-once")
    configure_logging(settings.service_name, level=settings.log_level)

    db = SignalDatabase(settings.database_url)
    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await db.connect()
        await redis_client.ping()
        consumer = SignalConsumer(settings, db, redis_client)
        return await consumer.run_once(block_ms=block_ms)
    finally:
        await db.close()
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
