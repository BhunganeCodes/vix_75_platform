"""Pre-trade validation: broker constraints, stops level, margin headroom.

Account and symbol data are read from Redis cache keys populated by the
MT5 bridge (data/execution services):

* ``mt5:account_info``  -> {"balance", "equity", "margin_free"}
* ``mt5:symbol:<sym>``  -> SymbolConstraints field set

Missing snapshots fail CLOSED (audit lesson: never assume defaults for
money-moving decisions).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import redis.asyncio as aioredis
from vix_core.logging import get_logger
from vix_core.risk import SymbolConstraints, validate_stop_distances
from vix_core.schemas import Signal

logger = get_logger(__name__)

ACCOUNT_KEY = "mt5:account_info"
SYMBOL_KEY_TEMPLATE = "mt5:symbol:{symbol}"

REQUIRED_SPEC_FIELDS = (
    "volume_min",
    "volume_max",
    "volume_step",
    "tick_size",
    "tick_value",
    "point",
    "stops_level_points",
    "margin_per_lot",
)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    balance: float
    equity: float
    margin_free: float


class RiskValidator:
    """Loads cached market/account context and applies hard gates."""

    def __init__(self, redis_client: aioredis.Redis | None) -> None:
        self._redis = redis_client

    # ------------------------------------------------------------------

    async def load_account(self) -> AccountSnapshot | None:
        raw = await self._get(ACCOUNT_KEY)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return AccountSnapshot(
                balance=float(data["balance"]),
                equity=float(data["equity"]),
                margin_free=float(data["margin_free"]),
            )
        except (KeyError, TypeError, ValueError):
            logger.exception("malformed account snapshot", key=ACCOUNT_KEY)
            return None

    async def load_symbol_constraints(self, symbol: str) -> SymbolConstraints | None:
        raw = await self._get(SYMBOL_KEY_TEMPLATE.format(symbol=symbol))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            missing = [f for f in REQUIRED_SPEC_FIELDS if f not in data]
            if missing:
                logger.error("symbol spec incomplete", missing=missing)
                return None
            return SymbolConstraints(
                volume_min=float(data["volume_min"]),
                volume_max=float(data["volume_max"]),
                volume_step=float(data["volume_step"]),
                tick_size=float(data["tick_size"]),
                tick_value=float(data["tick_value"]),
                point=float(data["point"]),
                stops_level_points=int(data["stops_level_points"]),
                margin_per_lot=float(data["margin_per_lot"]),
            )
        except (KeyError, TypeError, ValueError):
            logger.exception("malformed symbol spec", symbol=symbol)
            return None

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    @staticmethod
    def check_stops(signal: Signal, constraints: SymbolConstraints) -> str | None:
        """Rejection detail when SL/TP violate broker stop distances."""
        return validate_stop_distances(
            entry=signal.entry,
            stop_loss=signal.sl,
            take_profit=signal.tp1,
            constraints=constraints,
        )

    # ------------------------------------------------------------------

    async def _get(self, key: str) -> str | None:
        if self._redis is None:
            return None
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)
