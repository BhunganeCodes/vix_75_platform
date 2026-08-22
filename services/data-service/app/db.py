"""Async TimescaleDB access for data-service (psycopg3 async pipeline).

All writes are idempotent upserts so replays/backfills never duplicate rows.
The MT5 API itself remains synchronous; callers offload it with
``asyncio.to_thread`` while DB I/O stays on the event loop natively.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import psycopg
from psycopg.types.json import Jsonb
from vix_core.logging import get_logger
from vix_core.schemas import Bar

logger = get_logger(__name__)

_UPSERT_SQL = """
INSERT INTO ohlcv (symbol, timeframe, ts, open, high, low, close,
                   tick_volume, real_volume, spread, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, timeframe, ts) DO NOTHING
"""


class Database:
    """Single-connection async wrapper with lazy reconnect."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection[dict[str, object]] | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None and not self._conn.closed:
                return
            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            logger.info("database connected")

    async def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None
        logger.info("database disconnected")

    async def _ensure(self) -> psycopg.AsyncConnection[dict[str, object]]:
        if self._conn is None or self._conn.closed:
            logger.warning("database connection lost; reconnecting")
            await self.connect()
        assert self._conn is not None  # noqa: S101 - narrow for mypy after ensure
        return self._conn

    async def ping(self) -> bool:
        try:
            conn = await self._ensure()
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                return (await cur.fetchone()) is not None
        except (RuntimeError, psycopg.Error):
            logger.exception("database ping failed")
            return False

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @staticmethod
    def _bar_row(symbol: str, timeframe: str, bar: Bar) -> tuple[object, ...]:
        return (
            symbol,
            timeframe,
            bar.ts,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.tick_volume,
            0,
            0,
            "mt5",
        )

    async def upsert_bars(self, symbol: str, timeframe: str, bars: Sequence[Bar]) -> int:
        """Bulk-insert closed bars idempotently; returns rows attempted."""
        payload = [self._bar_row(symbol, timeframe, bar) for bar in bars]
        if not payload:
            return 0
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.executemany(_UPSERT_SQL, payload)
        return len(payload)

    async def audit(
        self,
        actor: str,
        action: str,
        subject: str,
        outcome: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Append an audit_log row (never blocks the caller on failure)."""
        try:
            conn = await self._ensure()
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO audit_log (actor, action, subject, outcome, payload)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (actor, action, subject, outcome, Jsonb(payload or {})),
                )
        except psycopg.Error:
            logger.exception("audit write failed", action=action)
