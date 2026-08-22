"""Data-service MT5 bridge client.

Extends :class:`vix_core.mt5_client.MT5Client` (which already owns retry,
tz-awareness and graceful shutdown) with ingestion-specific capabilities:

* M1 polling support.
* Chunked history iteration for multi-year backfills without loading the
  full range into memory (10k bars per chunk).

The raw MetaTrader5 API is synchronous; every call here is expected to be
offloaded via ``asyncio.to_thread`` by callers.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType

import numpy as np
import numpy.typing as npt
from vix_core.logging import get_logger
from vix_core.mt5_client import MT5Client
from vix_core.schemas import Bar

logger = get_logger(__name__)

__all__ = ["BridgeMT5Client", "RatesRow"]

RatesRow = np.void  # structured numpy row from copy_rates_*

INGEST_TIMEFRAMES: tuple[str, ...] = ("M1", "M5", "M15", "H1")


class BridgeMT5Client(MT5Client):
    """Ingestion-facing wrapper around the shared MT5 client."""

    def fetch_latest_bars(
        self,
        symbol: str,
        timeframes: tuple[str, ...] = INGEST_TIMEFRAMES,
        count: int = 300,
    ) -> dict[str, tuple[Bar, ...]]:
        """Fetch the most recent closed bars for every polled timeframe."""
        return {tf: self.copy_bars(symbol, tf, count) for tf in timeframes}

    def iter_history_chunks(
        self,
        symbol: str,
        timeframe: str,
        *,
        lookback_days: int,
        chunk_size: int = 10_000,
    ) -> Iterator[npt.NDArray[np.void]]:
        """Yield ascending chunks of historical rates, newest first pages.

        Pages backwards through terminal history using
        ``copy_rates_from_pos`` so memory stays bounded at ``chunk_size``
        rows regardless of lookback depth. Stops when either the lookback
        window is exhausted or the terminal returns no further bars.
        """
        mt5 = self.require_module()
        if timeframe not in INGEST_TIMEFRAMES and timeframe not in {"M30", "H4", "D1"}:
            raise ValueError(f"unsupported backfill timeframe {timeframe!r}")

        from vix_core.mt5_client import TIMEFRAME_MAP

        tf_const = TIMEFRAME_MAP[timeframe]
        cutoff = datetime.now(tz=UTC) - timedelta(days=lookback_days)
        start_pos = 0
        total = 0

        while True:
            rates = mt5.copy_rates_from_pos(symbol, tf_const, start_pos, chunk_size)
            if rates is None or len(rates) == 0:
                break
            total += len(rates)
            yield rates
            oldest_ts = datetime.fromtimestamp(int(rates[0]["time"]), tz=UTC)
            if oldest_ts <= cutoff or len(rates) < chunk_size:
                logger.info(
                    "history exhausted",
                    symbol=symbol,
                    timeframe=timeframe,
                    bars=total,
                    oldest=str(oldest_ts),
                )
                return
            start_pos += len(rates)

    def require_module(self) -> ModuleType:
        from vix_core.mt5_client import require_mt5

        return require_mt5()
