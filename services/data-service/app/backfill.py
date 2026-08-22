"""Chunked historical backfill: MT5 -> ohlcv hypertable.

Used by both ``POST /backfill`` (in-service, Windows bridge host) and
``scripts/fetch_history.py`` (standalone CLI). Bars are fetched in bounded
chunks (default 10k) so a 5-year M1 backfill never spikes memory.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
from vix_core.logging import get_logger
from vix_core.schemas import Bar

from .db import Database
from .mt5_client import BridgeMT5Client

logger = get_logger(__name__)

ProgressCb = Callable[[str, int], None]


def rows_to_bars(rates: npt.NDArray[np.void]) -> tuple[Bar, ...]:
    """Convert structured numpy rates to tz-aware Bar models."""
    from datetime import UTC, datetime

    return tuple(
        Bar(
            ts=datetime.fromtimestamp(int(row["time"]), tz=UTC),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_volume=int(row["tick_volume"]),
        )
        for row in rates
    )


async def backfill_timeframe(
    *,
    client: BridgeMT5Client,
    db: Database,
    symbol: str,
    timeframe: str,
    lookback_days: int,
    chunk_size: int = 10_000,
    progress: ProgressCb | None = None,
) -> int:
    """Fetch and store history for one timeframe; returns bars stored."""
    total = 0
    for chunk in client.iter_history_chunks(
        symbol, timeframe, lookback_days=lookback_days, chunk_size=chunk_size
    ):
        bars = rows_to_bars(chunk)
        if not bars:
            continue
        total += await db.upsert_bars(symbol, timeframe, bars)
        if progress is not None:
            progress(timeframe, len(bars))
    logger.info(
        "backfill complete",
        symbol=symbol,
        timeframe=timeframe,
        bars_stored=total,
    )
    await db.audit(
        actor="data-service",
        action="backfill",
        subject=f"{symbol}:{timeframe}",
        outcome="ok",
        payload={"bars": total, "lookback_days": lookback_days},
    )
    return total
