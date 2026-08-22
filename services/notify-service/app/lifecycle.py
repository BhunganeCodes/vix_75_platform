"""Signal-lifecycle audit trail: audit_log inserts + structured logging.

Every lifecycle event is recorded twice: once as a structlog line bound to
its correlation id (searchable in log aggregation) and once as an
append-only ``audit_log`` row (queryable history).
"""

from __future__ import annotations

from typing import cast

import psycopg
from psycopg.types.json import Jsonb
from vix_core.logging import get_logger

logger = get_logger(__name__)

_INSERT_AUDIT_SQL = """
INSERT INTO audit_log (actor, action, subject, outcome, payload)
VALUES (%s, %s, %s, %s, %s)
"""


class LifecycleLogger:
    """Async audit_log writer for notify-service events."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None
        self.recorded = 0

    async def connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)

    async def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

    async def record(
        self,
        *,
        event: str,
        subject: str,
        outcome: str = "ok",
        payload: dict[str, object] | None = None,
    ) -> None:
        logger.info(
            "signal lifecycle recorded",
            lifecycle_event=event,
            subject=subject,
            outcome=outcome,
        )
        try:
            conn = cast(psycopg.AsyncConnection, await self._ensure())
            async with conn.cursor() as cur:
                await cur.execute(
                    _INSERT_AUDIT_SQL,
                    ("notify-service", event, subject, outcome, Jsonb(payload or {})),
                )
            self.recorded += 1
        except psycopg.Error:
            logger.exception("audit insert failed", lifecycle_event=event)

    async def ping(self) -> bool:
        try:
            conn = cast(psycopg.AsyncConnection, await self._ensure())
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                return (await cur.fetchone()) is not None
        except (RuntimeError, psycopg.Error):
            logger.exception("database ping failed")
            return False

    async def _ensure(self) -> psycopg.AsyncConnection | None:
        if self._conn is None or self._conn.closed:
            await self.connect()
        return self._conn
