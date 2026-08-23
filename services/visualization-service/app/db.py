"""Async TimescaleDB queries for visualization-service (psycopg3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
from vix_core.logging import get_logger

logger = get_logger(__name__)

_OHLCV_SQL = """
SELECT ts, open, high, low, close, tick_volume
FROM ohlcv
WHERE symbol = %s AND timeframe = %s AND ts >= %s
ORDER BY ts ASC
"""

_FEATURES_SQL = """
SELECT ts, close, regime_id, zones
FROM features
WHERE symbol = %s AND timeframe = %s AND ts >= %s
ORDER BY ts ASC
"""

_SIGNALS_SQL = """
SELECT id, created_ts, direction, entry, sl, tp1, tp2, score,
       max_score, p_win, status
FROM signals
WHERE symbol = %s AND created_ts >= %s
ORDER BY created_ts DESC
LIMIT 500
"""


def _to_cols(rows: list[tuple[object, ...]], columns: list[str]) -> dict[str, list[Any]]:
    return {col: [row[i] for row in rows] for i, col in enumerate(columns)}


class VizDatabase:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            logger.info("viz database connected")

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

    async def fetch_ohlcv(self, symbol: str, timeframe: str, days: int) -> dict[str, list[Any]]:
        since = datetime.now(tz=UTC) - timedelta(days=days)
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_OHLCV_SQL, (symbol, timeframe, since))
            rows = await cur.fetchall()
        return _to_cols(rows, ["ts", "open", "high", "low", "close", "volume"])

    async def fetch_features_with_regime(
        self, symbol: str, timeframe: str, days: int
    ) -> dict[str, list[Any]]:
        since = datetime.now(tz=UTC) - timedelta(days=days)
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_FEATURES_SQL, (symbol, timeframe, since))
            rows = await cur.fetchall()
        return _to_cols(rows, ["ts", "close", "regime_id", "zones"])

    async def fetch_signals(self, symbol: str, days: int) -> dict[str, list[Any]]:
        since = datetime.now(tz=UTC) - timedelta(days=days)
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_SIGNALS_SQL, (symbol, since))
            rows = await cur.fetchall()
        return _to_cols(
            rows,
            [
                "id",
                "created_ts",
                "direction",
                "entry",
                "sl",
                "tp1",
                "tp2",
                "score",
                "max_score",
                "p_win",
                "status",
            ],
        )
