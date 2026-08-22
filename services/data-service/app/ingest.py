"""Background ingestion worker: MT5 -> TimescaleDB -> Redis Stream.

Pipeline per poll cycle:

1. Poll closed bars for M1/M5/M15/H1 (MT5 calls offloaded to threads).
2. Bulk-upsert new bars into the ``ohlcv`` hypertable.
3. Publish one ``ohlcv.update`` stream entry per closed bar with fields
   ``{symbol, timeframe, ts, close, correlation_id}``.

On Oracle Cloud (no MT5 package) the worker stands by and keeps retrying,
so the same image serves both the local Windows bridge and cloud dev.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

import redis.asyncio as aioredis
from vix_core.config import Settings
from vix_core.correlation import stream_fields
from vix_core.logging import get_logger
from vix_core.mt5_client import MT5UnavailableError
from vix_core.schemas import Bar

from .db import Database
from .mt5_client import INGEST_TIMEFRAMES, BridgeMT5Client

logger = get_logger(__name__)

OHLCV_STREAM = "ohlcv.update"


@dataclass(slots=True)
class IngestStats:
    cycles: int = 0
    bars_written: int = 0
    events_published: int = 0
    last_bar_ts: str | None = None
    errors: int = 0


@dataclass(slots=True)
class Ingestor:
    """Polls MT5 on candle boundaries; owns DB + stream publishing."""

    settings: Settings
    db: Database
    redis: aioredis.Redis
    client: BridgeMT5Client
    timeframes: tuple[str, ...] = field(default_factory=lambda: INGEST_TIMEFRAMES)
    history_bars: int = 300
    stats: IngestStats = field(default_factory=IngestStats)
    _last_seen: dict[str, str] = field(default_factory=dict)

    @property
    def mt5_available(self) -> bool:
        try:
            from vix_core.mt5_client import require_mt5

            require_mt5()
        except MT5UnavailableError:
            return False
        else:
            return True

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        symbol = self.settings.symbol
        logger.info(
            "ingest loop starting",
            symbol=symbol,
            timeframes=list(self.timeframes),
        )
        while True:
            try:
                await self.cycle(symbol)
            except MT5UnavailableError:
                self.stats.errors += 1
                logger.warning("mt5 unavailable; standing by", retry_in_s=30)
                await asyncio.sleep(30)
            except Exception:
                self.stats.errors += 1
                logger.exception("ingest cycle failed")
                await asyncio.sleep(self.settings.poll_interval_seconds)
            else:
                await asyncio.sleep(self.settings.poll_interval_seconds)

    async def cycle(self, symbol: str | None = None) -> None:
        """One polling pass across all timeframes (exposed for tests)."""
        symbol = symbol or self.settings.symbol
        if not self.client._connected:
            await asyncio.to_thread(self.client.connect)

        for timeframe in self.timeframes:
            bars = await asyncio.to_thread(
                self.client.copy_bars, symbol, timeframe, self.history_bars
            )
            fresh = self._filter_new(timeframe, bars)
            if not fresh:
                continue
            written = await self.db.upsert_bars(symbol, timeframe, fresh)
            self.stats.bars_written += written
            await self._publish(symbol, timeframe, fresh)

        self.stats.cycles += 1

    # ------------------------------------------------------------------

    def _filter_new(self, timeframe: str, bars: tuple[Bar, ...]) -> tuple[Bar, ...]:
        """First poll forwards the whole window (DB catch-up); afterwards
        only bars strictly newer than the last processed close."""
        if not bars:
            return ()
        latest_key = bars[-1].ts.isoformat()
        previous = self._last_seen.get(timeframe)
        self._last_seen[timeframe] = latest_key
        if previous is None:
            return bars
        return tuple(b for b in bars if b.ts.isoformat() > previous)

    async def _publish(self, symbol: str, timeframe: str, bars: tuple[Bar, ...]) -> None:
        """XADD one entry per closed bar onto the ohlcv.update stream."""
        pipeline = self.redis.pipeline(transaction=False)
        last_iso: str | None = None
        for bar in bars:
            last_iso = bar.ts.isoformat()
            payload = {
                "symbol": symbol,
                "timeframe": timeframe,
                "ts": bar.ts.isoformat(),
                "close": bar.close,
            }
            entry = cast(dict[Any, Any], stream_fields(payload))
            pipeline.xadd(OHLCV_STREAM, entry)
        await pipeline.execute()
        self.stats.events_published += len(bars)
        self.stats.last_bar_ts = last_iso
        logger.debug(
            "bars published",
            timeframe=timeframe,
            count=len(bars),
            stream=OHLCV_STREAM,
        )


async def drain_stream(
    redis: aioredis.Redis,
    stream: str = OHLCV_STREAM,
    count: int = 1000,
) -> int:
    """Utility used by maintenance/tests to trim old stream entries."""
    removed = await redis.xtrim(stream, maxlen=count, approximate=False)
    return int(removed)
