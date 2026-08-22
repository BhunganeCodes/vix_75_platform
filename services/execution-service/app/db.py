"""Async TimescaleDB access for execution-service (psycopg3).

The ``trades.idempotency_key`` UNIQUE constraint is the authoritative
dedupe backstop behind the Redis fast-path: concurrent consumers may both
attempt an insert but only one wins.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb
from vix_core.logging import get_logger
from vix_core.schemas import OrderRequest, uuid_from_hex

logger = get_logger(__name__)

_INSERT_PENDING_SQL = """
INSERT INTO trades (idempotency_key, signal_id, symbol, side, lots,
                    entry_price, sl_price, tp_price, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
ON CONFLICT (idempotency_key) DO NOTHING
"""

_SET_STATUS_SQL = {
    "submitted": """
        UPDATE trades SET status='submitted', retcode=%s, retcode_desc=%s,
               raw_response=COALESCE(raw_response,'{}'::jsonb) || %s
        WHERE idempotency_key=%s""",
    "filled": """
        UPDATE trades SET status='filled', retcode=%s, retcode_desc=%s,
               broker_ticket=%s, entry_price=COALESCE(%s, entry_price),
               executed_at=now(),
               raw_response=COALESCE(raw_response,'{}'::jsonb) || %s
        WHERE idempotency_key=%s""",
    "rejected": """
        UPDATE trades SET status='rejected', retcode=%s, retcode_desc=%s,
               raw_response=COALESCE(raw_response,'{}'::jsonb) || %s
        WHERE idempotency_key=%s""",
    "cancelled": """
        UPDATE trades SET status='cancelled', retcode_desc=%s
        WHERE idempotency_key=%s AND status='pending'""",
}

_CLOSE_SQL = """
UPDATE trades SET closed_at=%s, profit=%s,
       raw_response=COALESCE(raw_response,'{}'::jsonb) || %s
WHERE broker_ticket=%s AND closed_at IS NULL
"""

_OPEN_TRADES_SQL = """
SELECT idempotency_key, signal_id, symbol, side, lots, entry_price,
       sl_price, tp_price, broker_ticket, requested_at
FROM trades
WHERE status='filled' AND closed_at IS NULL
ORDER BY requested_at DESC
"""

_BY_TICKET_SQL = """
SELECT idempotency_key, symbol, side, lots, entry_price, sl_price, tp_price
FROM trades
WHERE broker_ticket=%s AND closed_at IS NULL
"""


class ExecutionDatabase:
    """Single-connection async wrapper with lazy reconnect."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            logger.info("execution database connected")

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
    # Lifecycle writes
    # ------------------------------------------------------------------

    async def insert_pending(self, order: OrderRequest) -> bool:
        """Insert a pending trade; False when the key already exists."""
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(
                _INSERT_PENDING_SQL,
                (
                    order.idempotency_key,
                    uuid_from_hex(order.signal_id),
                    order.symbol,
                    str(order.direction),
                    order.lots,
                    order.entry,
                    order.sl,
                    order.tp,
                ),
            )
            inserted = cur.rowcount == 1
        if not inserted:
            logger.warning("duplicate trade insert suppressed", key=order.idempotency_key)
        return inserted

    async def set_status(
        self,
        status: str,
        key: str,
        *,
        retcode: int | None = None,
        desc: str | None = None,
        ticket: int | None = None,
        price: float | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        sql = _SET_STATUS_SQL[status]
        raw = Jsonb(extra or {})
        conn = await self._ensure()
        async with conn.cursor() as cur:
            if status == "filled":
                await cur.execute(sql, (retcode, desc, ticket, price, raw, key))
            elif status in ("submitted", "rejected"):
                await cur.execute(sql, (retcode, desc, raw, key))
            else:  # cancelled
                await cur.execute(sql, (desc, key))

    async def close_by_ticket(
        self,
        broker_ticket: int,
        *,
        profit: float,
        exit_price: float | None,
        extra: dict[str, object] | None = None,
    ) -> bool:
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(
                _CLOSE_SQL,
                (
                    datetime.now(tz=UTC),
                    profit,
                    Jsonb({"exit_price": exit_price, **(extra or {})}),
                    broker_ticket,
                ),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def open_trades(self) -> list[dict[str, Any]]:
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_OPEN_TRADES_SQL)
            rows = await cur.fetchall()
        keys = [
            "idempotency_key",
            "signal_id",
            "symbol",
            "side",
            "lots",
            "entry_price",
            "sl_price",
            "tp_price",
            "broker_ticket",
            "requested_at",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    async def trade_by_ticket(self, broker_ticket: int) -> dict[str, Any] | None:
        conn = await self._ensure()
        async with conn.cursor() as cur:
            await cur.execute(_BY_TICKET_SQL, (broker_ticket,))
            row = await cur.fetchone()
        if row is None:
            return None
        keys = ["idempotency_key", "symbol", "side", "lots", "entry_price", "sl_price", "tp_price"]
        return dict(zip(keys, row, strict=True))

    async def audit(
        self,
        actor: str,
        action: str,
        subject: str,
        outcome: str,
        payload: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        try:
            conn = await self._ensure()
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO audit_log (actor, action, subject, outcome, payload, error)"
                    " VALUES (%s,%s,%s,%s,%s,%s)",
                    (actor, action, subject, outcome, Jsonb(payload or {}), error),
                )
        except psycopg.Error:
            logger.exception("audit write failed", action=action)


def dumps(value: object) -> str:
    return json.dumps(value, default=str)
