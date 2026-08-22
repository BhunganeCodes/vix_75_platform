"""One-shot consumer runner: `python -m app.consumer_once [--block-ms N]`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

if sys.platform == "win32":  # pragma: no cover - psycopg3 n/a here but consistent
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import redis.asyncio as aioredis
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

from .consumer import RiskConsumer

logger = get_logger(__name__)


async def _run(block_ms: int) -> int:
    settings = Settings(service_name="risk-consumer-once")
    configure_logging(settings.service_name, level=settings.log_level)

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        consumer = RiskConsumer(settings, redis_client)
        return await consumer.run_once(block_ms=block_ms)
    finally:
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
