"""Sync TimescaleDB helpers for ml-service training/inference.

ML workloads are batch-shaped (pull a few thousand feature rows, train,
write one row) so synchronous psycopg executed via ``asyncio.to_thread``
at the route boundary is the pragmatic choice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
import psycopg
from psycopg.types.json import Jsonb  # noqa: F401 - re-exported for callers
from vix_core.logging import get_logger

logger = get_logger(__name__)

FEATURE_MATRIX_SQL = """
SELECT ts, close, atr, atr_norm, rsi, ema50, ema200,
       bb_upper, bb_mid, bb_lower, stoch_k, stoch_d,
       realized_vol, log_return
FROM features
WHERE symbol = %s AND timeframe = %s
ORDER BY ts ASC
"""

OHLCV_WITH_ATR_SQL = """
SELECT ts, close, atr
FROM features
WHERE symbol = %s AND timeframe = %s AND atr IS NOT NULL
ORDER BY ts ASC
"""

SAVE_REGIME_SQL = """
UPDATE features
SET regime_id = %s, regime_probs = %s
WHERE symbol = %s AND timeframe = %s AND ts = %s
"""


def fetch_feature_matrix(
    dsn: str, symbol: str, timeframe: str, limit: int | None = None
) -> tuple[npt.NDArray[np.datetime64], list[str], npt.NDArray[np.float64]]:
    """Numeric feature matrix ordered by time; NaN rows dropped.

    Returns (timestamps, column_names, matrix).
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(FEATURE_MATRIX_SQL, (symbol, timeframe))
        rows = cur.fetchall() if limit is None else cur.fetchall()[-limit:]
    if not rows:
        return np.empty(0, dtype="datetime64[ns]"), [], np.empty((0, 0))

    columns = [
        "close",
        "atr",
        "atr_norm",
        "rsi",
        "ema50",
        "ema200",
        "bb_upper",
        "bb_mid",
        "bb_lower",
        "stoch_k",
        "stoch_d",
        "realized_vol",
        "log_return",
    ]
    timestamps = np.array([row[0] for row in rows], dtype="datetime64[ns]")
    raw = np.array(
        [[float(v) if v is not None else np.nan for v in row[1:]] for row in rows],
        dtype=np.float64,
    )
    complete = ~np.any(np.isnan(raw), axis=1)
    return timestamps[complete], columns, raw[complete]


def fetch_close_atr(
    dsn: str, symbol: str, timeframe: str
) -> tuple[npt.NDArray[np.datetime64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Close + ATR series used for triple-barrier labeling."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(OHLCV_WITH_ATR_SQL, (symbol, timeframe))
        rows = cur.fetchall()
    ts = np.array([r[0] for r in rows], dtype="datetime64[ns]")
    close = np.array([float(r[1]) for r in rows], dtype=np.float64)
    atr_arr = np.array([float(r[2]) for r in rows], dtype=np.float64)
    return ts, close, atr_arr


def save_regime_batch(
    dsn: str,
    symbol: str,
    timeframe: str,
    stamps: Sequence,
    ids: Sequence[int],
    probs: Sequence[Sequence[float]],
) -> int:
    """Backfill regime columns for a batch of timestamps."""
    payload = [
        (int(state_id), list(prob), symbol, timeframe, stamp)
        for stamp, state_id, prob in zip(stamps, ids, probs, strict=True)
    ]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.executemany(SAVE_REGIME_SQL, payload)
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(payload)
    logger.info("regime labels saved", timeframe=timeframe, rows=written)
    return int(written)


class FeatureDatabaseML:
    """Thin connection holder so the app lifecycle can ping/close cleanly."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @property
    def dsn(self) -> str:
        return self._dsn

    async def ping(self) -> bool:
        try:
            await asyncio.to_thread(_ping_sync, self._dsn)
        except psycopg.Error:
            logger.exception("database ping failed")
            return False
        else:
            return True


def _ping_sync(dsn: str) -> None:
    with psycopg.connect(dsn, connect_timeout=3) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")


__all__ = [
    "FeatureDatabaseML",
    "fetch_close_atr",
    "fetch_feature_matrix",
    "save_regime_batch",
]
