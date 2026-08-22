"""Async TimescaleDB access for signal-service (psycopg3)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb
from vix_core.logging import get_logger
from vix_core.schemas import Signal, Zone, uuid_from_hex

from .engine import LtfSnapshot

logger = get_logger(__name__)

_FETCH_FEATURE_SQL = """
SELECT ts, close, atr, rsi, ema50, ema200, bb_mid, stoch_k, zones
FROM features
WHERE symbol = %s AND timeframe = %s
ORDER BY ts DESC
LIMIT 1
"""

_INSERT_SIGNAL_SQL = """
INSERT INTO signals (
    id, created_ts, symbol, ltf_timeframe, direction,
    entry, sl, tp1, tp2, score, max_score, components, p_win, status
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'proposed'
)
ON CONFLICT (id) DO NOTHING
"""

_HISTORY_SQL = """
SELECT id, created_ts, symbol, ltf_timeframe, direction,
       entry, sl, tp1, tp2, score, max_score, p_win, status
FROM signals
WHERE (%s::text IS NULL OR symbol = %s)
ORDER BY created_ts DESC
LIMIT %s
"""


def _row_to_snapshot(symbol: str, timeframe: str, row: tuple[Any, ...]) -> LtfSnapshot | None:
    """Map a feature row to an engine LtfSnapshot (zones parsed)."""
    ts = cast(datetime, row[0])
    zones_raw = row[8]
    if isinstance(zones_raw, str):
        zones_data = json.loads(zones_raw)
    else:
        zones_data = zones_raw or []
    zones = tuple(Zone.model_validate(z) for z in zones_data)

    return LtfSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        ts=ts,
        close=float(row[1]),
        atr=float(row[2]),
        rsi=float(row[3]),
        ema50=float(row[4]),
        ema200=float(row[5]),
        zones=zones,
    )


class SignalDatabase:
    """Single-connection async wrapper with lazy reconnect."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            logger.info("signal database connected")

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

    # ------------------------------------------------------------------

    async def fetch_ltf_row(self, symbol: str, timeframe: str) -> LtfSnapshot | None:
        """Latest stored LTF feature row as an engine snapshot."""
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_FETCH_FEATURE_SQL, (symbol, timeframe))
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_snapshot(symbol, timeframe, row)

    async def fetch_htf_trend(self, symbol: str, timeframes: tuple[str, ...]) -> str:
        """ "up"/"down"/"flat" voted across HTF rows (D1 preferred)."""
        from app.engine import htf_trend_from_emas

        votes: list[str] = []
        conn = await self._ensure()
        async with conn.cursor() as cur:
            for timeframe in timeframes:
                await cur.execute(_FETCH_FEATURE_SQL, (symbol, timeframe))
                row = await cur.fetchone()
                if row is None or row[4] is None or row[5] is None:
                    continue
                votes.append(htf_trend_from_emas(float(row[4]), float(row[5])))

        if "up" in votes and "down" not in votes:
            return "up"
        if "down" in votes and "up" not in votes:
            return "down"
        if votes:
            return votes[0]
        return "flat"

    async def insert_signal(self, signal: Signal) -> bool:
        conn = await self._ensure()
        components_json = Jsonb(signal.components.model_dump(mode="json"))
        async with conn.cursor() as cur:
            await cur.execute(
                _INSERT_SIGNAL_SQL,
                (
                    uuid_from_hex(signal.id),
                    signal.created_ts,
                    signal.symbol,
                    signal.ltf_timeframe,
                    signal.direction.value,
                    signal.entry,
                    signal.sl,
                    signal.tp1,
                    signal.tp2,
                    signal.score,
                    signal.max_score,
                    components_json,
                    signal.p_win,
                ),
            )
            return cur.rowcount == 1

    async def history(self, symbol: str | None, limit: int = 50) -> list[dict[str, object]]:
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_HISTORY_SQL, (symbol, symbol, limit))
            rows = await cur.fetchall()
        keys = [
            "id",
            "created_ts",
            "symbol",
            "ltf_timeframe",
            "direction",
            "entry",
            "sl",
            "tp1",
            "tp2",
            "score",
            "max_score",
            "p_win",
            "status",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]
