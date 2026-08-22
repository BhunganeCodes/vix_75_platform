"""Shared doubles for gateway tests."""

from __future__ import annotations

import itertools
import time
from typing import Any


class FakeRedis:
    """Async double covering the ZSET/KV surface the limiter uses."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.kv: dict[str, str] = {}
        self._seq = itertools.count()

    # -- sorted sets ------------------------------------------------------

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zremrangebyscore(self, key: str, min_: Any, max_: Any) -> int:
        """Redis semantics: REMOVE members with min <= score <= max."""
        zs = self.zsets.setdefault(key, {})
        min_f = float(min_)
        max_f = float(max_)
        doomed = [m for m, s in zs.items() if min_f <= s <= max_f]
        for m in doomed:
            del zs[m]
        return len(doomed)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zrange(self, key: str, start: int, stop: int, withscores: bool = False) -> list[Any]:
        members = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        sliced = members[start : stop + 1 if stop >= 0 else None]
        if withscores:
            return [(m, s) for m, s in sliced]
        return [m for m, _ in sliced]

    async def expire(self, key: str, seconds: int) -> bool:
        return key in self.zsets or key in self.kv

    # -- kv ----------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.kv[key] = value
        return True


def monotonic() -> float:
    return time.monotonic()
