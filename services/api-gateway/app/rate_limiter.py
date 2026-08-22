"""Sliding-window rate limiter backed by Redis sorted sets.

Per JWT subject: a ZSET holds one member per observed request scored by
epoch-ms. Each check prunes members older than the window, records the
new request, counts survivors, and rejects when over the limit. Because
membership is scored on actual arrival time there are no fixed-window
boundary bursts.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from vix_core.logging import get_logger

logger = get_logger(__name__)

KEY_TEMPLATE = "rate:{subject}"


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after_s: int


class SlidingWindowLimiter:
    """Redis ZSET sliding window; construct with limit + window size."""

    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        *,
        limit_per_window: int = 100,
        window_seconds: int = 60,
    ) -> None:
        self._redis = redis_client
        self._limit = limit_per_window
        self._window_ms = window_seconds * 1_000
        self._window_s = window_seconds

    async def check(self, subject: str) -> RateDecision:
        if self._redis is None:
            return RateDecision(allowed=True, remaining=self._limit, retry_after_s=0)

        client = cast_any(self._redis)
        key = KEY_TEMPLATE.format(subject=subject)
        now_ms = int(time.time() * 1000)
        window_start = now_ms - self._window_ms
        member = f"{now_ms}-{uuid.uuid4().hex[:8]}"

        # Prune expired members, record this request, count the window.
        await client.zremrangebyscore(key, "-inf", window_start)
        await client.zadd(key, {member: now_ms})
        count = int(await client.zcard(key))
        await client.expire(key, self._window_s)

        if count <= self._limit:
            logger.debug("rate ok", subject=subject, count=count)
            return RateDecision(
                allowed=True, remaining=max(self._limit - count, 0), retry_after_s=0
            )

        oldest = await client.zrange(key, 0, 0, withscores=True)
        oldest_ms = float(oldest[0][1]) if oldest else float(window_start)
        retry_after_s = max(1, math.ceil((oldest_ms + self._window_ms - now_ms) / 1000))
        logger.warning(
            "rate limit exceeded",
            subject=subject,
            count=count,
            retry_after_s=retry_after_s,
        )
        return RateDecision(allowed=False, remaining=0, retry_after_s=retry_after_s)


def cast_any(value: Any) -> Any:
    """redis-py stubs union sync/async returns; tests also pass doubles."""
    return value
