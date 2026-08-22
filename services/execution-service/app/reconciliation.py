"""Broker <-> local-store reconciliation loop.

Every ``reconcile_interval_seconds``:

1. Read MT5 open positions (``positions_get``).
2. Read locally-open filled trades (``closed_at IS NULL``).
3. Orphans (MT5 position without a local row) -> CRITICAL log; they were
   opened outside this system and must be investigated manually.
4. Ghosts (local row whose ticket vanished from MT5) -> pull the closing
   deals from ``history_deals_get(position=ticket)``, compute realised
   PnL + volume-weighted exit price, close the local row and publish
   ``order.closed`` so notify-service alerts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import redis.asyncio as aioredis
from vix_core.config import Settings
from vix_core.correlation import stream_fields
from vix_core.logging import get_logger

from .db import ExecutionDatabase

logger = get_logger(__name__)

STREAM_CLOSED = "order.closed"
DEAL_ENTRY_OUT = 1  # mt5.DEAL_ENTRY_OUT


class Reconciler:
    """Periodic broker/store consistency sweep."""

    def __init__(
        self,
        settings: Settings,
        db: ExecutionDatabase,
        redis: aioredis.Redis,
        *,
        mt5: Any | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._redis = redis
        if mt5 is None:
            from vix_core.mt5_client import require_mt5

            mt5 = require_mt5()
        self._mt5 = mt5

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        interval = self._settings.reconcile_interval_seconds
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reconciliation cycle failed")
            await asyncio.sleep(interval)

    async def run_once(self) -> dict[str, int]:
        """One sweep; returns counters (exposed for tests)."""
        module = self._mt5
        symbol = self._settings.symbol

        positions = await asyncio.to_thread(module.positions_get)
        mt5_tickets = {
            int(p.ticket): p for p in (positions or []) if getattr(p, "symbol", symbol) == symbol
        }

        local_rows = await self._db.open_trades()
        local_tickets = {
            int(row["broker_ticket"]): row for row in local_rows if row["broker_ticket"] is not None
        }
        no_ticket = [r for r in local_rows if r["broker_ticket"] is None]

        orphans = sorted(set(mt5_tickets) - set(local_tickets))
        ghosts = sorted(set(local_tickets) - set(mt5_tickets))

        for ticket in orphans:
            pos = mt5_tickets[ticket]
            logger.critical(
                "ORPHAN MT5 position not tracked locally",
                ticket=ticket,
                symbol=getattr(pos, "symbol", symbol),
                volume=float(getattr(pos, "volume", 0.0)),
                profit=float(getattr(pos, "profit", 0.0)),
            )

        closed_count = 0
        for ticket in ghosts:
            row = local_tickets[ticket]
            summary = await asyncio.to_thread(self._summarise_close, module, symbol, ticket)
            if summary is None:
                logger.warning(
                    "position missing in MT5 but no closing deal found yet",
                    ticket=ticket,
                    key=row["idempotency_key"],
                )
                continue
            exit_price, profit = summary
            updated = await self._db.close_by_ticket(
                ticket,
                profit=profit,
                exit_price=exit_price,
                extra={"source": "reconciliation"},
            )
            if not updated:
                continue
            closed_count += 1
            fields = cast(
                dict[str, object],
                stream_fields(
                    {
                        "signal_id": str(row["signal_id"]),
                        "idempotency_key": str(row["idempotency_key"]),
                        "symbol": symbol,
                        "ticket": ticket,
                        "pnl": profit,
                        "exit_price": exit_price or 0.0,
                    },
                    correlation_id=None,
                ),
            )
            await self._redis.xadd(STREAM_CLOSED, json.loads(json.dumps(fields)))
            logger.info(
                "local trade reconciled as closed",
                ticket=ticket,
                pnl=profit,
                key=row["idempotency_key"],
            )

        for row in no_ticket:
            logger.error(
                "filled trade has no broker ticket; cannot reconcile",
                key=str(row["idempotency_key"]),
            )

        return {"orphans": len(orphans), "ghosts_closed": closed_count}

    def _summarise_close(
        self, module: Any, symbol: str, ticket: int
    ) -> tuple[float | None, float] | None:
        """Volume-weighted exit price + realised PnL from deal history."""
        now = datetime.now(tz=UTC)
        deals = module.history_deals_get(now - timedelta(days=7), now, position=ticket) or []
        out_deals = [
            d
            for d in deals
            if _deal_field(d, "entry", None) == DEAL_ENTRY_OUT or _deal_field(d, "entry", None) == 1
        ]
        if not out_deals:
            return None

        total_volume = sum(float(_deal_field(d, "volume", 0.0)) for d in out_deals)
        notional = sum(
            float(_deal_field(d, "volume", 0.0)) * float(_deal_field(d, "price", 0.0))
            for d in out_deals
        )
        profit = sum(float(_deal_field(d, "profit", 0.0)) for d in out_deals)
        exit_price = notional / total_volume if total_volume > 0 else None
        return exit_price, round(profit, 2)


def _deal_field(deal: Any, name: str, default: Any = None) -> Any:
    if isinstance(deal, dict):
        return deal.get(name, default)
    return getattr(deal, name, default)
