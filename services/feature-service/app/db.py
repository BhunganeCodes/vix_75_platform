"""Async TimescaleDB access for feature-service (psycopg3)."""

from __future__ import annotations

from typing import cast

import psycopg
from psycopg.types.json import Jsonb
from vix_core.logging import get_logger
from vix_core.schemas import Bar

logger = get_logger(__name__)

_FETCH_BARS_SQL = """
SELECT ts, open, high, low, close, tick_volume
FROM ohlcv
WHERE symbol = %s AND timeframe = %s
ORDER BY ts DESC
LIMIT %s
"""

_UPSERT_FEATURE_SQL = """
INSERT INTO features (
    symbol, timeframe, ts, close, atr, atr_norm, rsi,
    ema50, ema200, bb_upper, bb_mid, bb_lower,
    stoch_k, stoch_d, realized_vol, log_return,
    swing_high, swing_low, zones
) VALUES (
    %(symbol)s, %(timeframe)s, %(ts)s, %(close)s, %(atr)s, %(atr_norm)s, %(rsi)s,
    %(ema50)s, %(ema200)s, %(bb_upper)s, %(bb_mid)s, %(bb_lower)s,
    %(stoch_k)s, %(stoch_d)s, %(realized_vol)s, %(log_return)s,
    %(swing_high)s, %(swing_low)s, %(zones)s
)
ON CONFLICT (symbol, timeframe, ts) DO UPDATE SET
    close = EXCLUDED.close,
    atr = EXCLUDED.atr,
    atr_norm = EXCLUDED.atr_norm,
    rsi = EXCLUDED.rsi,
    ema50 = EXCLUDED.ema50,
    ema200 = EXCLUDED.ema200,
    bb_upper = EXCLUDED.bb_upper,
    bb_mid = EXCLUDED.bb_mid,
    bb_lower = EXCLUDED.bb_lower,
    stoch_k = EXCLUDED.stoch_k,
    stoch_d = EXCLUDED.stoch_d,
    realized_vol = EXCLUDED.realized_vol,
    log_return = EXCLUDED.log_return,
    swing_high = EXCLUDED.swing_high,
    swing_low = EXCLUDED.swing_low,
    zones = EXCLUDED.zones
"""


class FeatureDatabase:
    """Single-connection async wrapper with lazy reconnect."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            logger.info("feature database connected")

    async def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

    async def ping(self) -> bool:
        try:
            conn = await self._ensure()
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                return (await cur.fetchone()) is not None
        except (RuntimeError, psycopg.Error):
            logger.exception("database ping failed")
            return False

    async def _ensure(self) -> psycopg.AsyncConnection:
        if self._conn is None or self._conn.closed:
            await self.connect()
        return cast(psycopg.AsyncConnection, self._conn)

    async def fetch_bars(self, symbol: str, timeframe: str, limit: int = 500) -> tuple[Bar, ...]:
        """Most recent ``limit`` bars for a stream key, ascending order."""
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_FETCH_BARS_SQL, (symbol, timeframe, limit))
            rows = await cur.fetchall()
        return tuple(
            Bar(
                ts=row[0],
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                tick_volume=int(row[5] or 0),
            )
            for row in reversed(rows)
        )

    async def upsert_feature_row(self, row: dict[str, object]) -> None:
        """Idempotently store one computed feature snapshot."""
        payload = dict(row)
        payload["zones"] = Jsonb(payload.get("zones") or [])
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_UPSERT_FEATURE_SQL, payload)
