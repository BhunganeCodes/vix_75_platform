"""Open-position exposure tracking (in-memory mirror + Redis source of truth).

Positions are registered when risk approves an OrderRequest and released
via :meth:`release`. NOTE (Sprint 3 scope): there is no execution feedback
loop yet - releases are driven by explicit calls, not broker fills. The
execution-service integration will close this gap.

Limits enforced:
* ``max_open_trades`` - absolute count of open risk positions.
* ``max_total_risk_pct`` - summed per-trade risk as % of balance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as aioredis
from vix_core.logging import get_logger
from vix_core.schemas import Direction, RejectionReason

logger = get_logger(__name__)

OPEN_KEY = "risk:open_positions"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    idempotency_key: str
    signal_id: str
    symbol: str
    direction: Direction
    lots: float
    entry: float
    sl: float
    risk_amount: float  # account currency
    opened_ts: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "idempotency_key": self.idempotency_key,
                "signal_id": self.signal_id,
                "symbol": self.symbol,
                "direction": str(self.direction),
                "lots": self.lots,
                "entry": self.entry,
                "sl": self.sl,
                "risk_amount": self.risk_amount,
                "opened_ts": self.opened_ts,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> OpenPosition:
        data = json.loads(raw)
        return cls(
            idempotency_key=str(data["idempotency_key"]),
            signal_id=str(data["signal_id"]),
            symbol=str(data["symbol"]),
            direction=Direction(str(data["direction"])),
            lots=float(data["lots"]),
            entry=float(data["entry"]),
            sl=float(data["sl"]),
            risk_amount=float(data["risk_amount"]),
            opened_ts=str(data["opened_ts"]),
        )


class ExposureTracker:
    """Fault-tolerant open-risk registry backed by a Redis hash."""

    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        *,
        max_open_trades: int = 3,
        max_total_risk_pct: float = 3.0,
    ) -> None:
        self._redis = redis_client
        self._max_trades = max_open_trades
        self._max_risk_pct = max_total_risk_pct
        self._local: dict[str, OpenPosition] = {}

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def open_count(self) -> int:
        return len(self._local)

    def total_open_risk(self) -> float:
        """Summed risk amount across tracked positions (account ccy)."""
        return sum(p.risk_amount for p in self._local.values())

    async def refresh(self) -> None:
        """Reload local mirror from Redis (multi-instance safety)."""
        if self._redis is None:
            return
        hgetall = cast(Any, self._redis).hgetall
        raw = await hgetall(OPEN_KEY)
        decoded = {
            k.decode() if isinstance(k, bytes) else str(k): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in cast(dict[Any, Any], raw).items()
        }
        self._local = {k: OpenPosition.from_json(v) for k, v in decoded.items()}

    async def preflight(self, *, balance: float) -> tuple[bool, RejectionReason | None]:
        await self.refresh()
        if self.open_count() >= self._max_trades:
            return False, RejectionReason.MAX_OPEN_TRADES_REACHED
        total_risk_pct = (self.total_open_risk() / balance * 100.0) if balance > 0 else 0.0
        if total_risk_pct >= self._max_risk_pct:
            return False, RejectionReason.MAX_TOTAL_RISK_EXCEEDED
        return True, None

    # ------------------------------------------------------------------

    async def register(self, position: OpenPosition) -> None:
        self._local[position.idempotency_key] = position
        if self._redis is not None:
            await cast(Any, self._redis).hset(
                OPEN_KEY, position.idempotency_key, position.to_json()
            )
        logger.info(
            "exposure registered",
            key=position.idempotency_key,
            symbol=position.symbol,
            lots=position.lots,
            open_count=self.open_count(),
        )

    async def release(self, idempotency_key: str) -> bool:
        removed = self._local.pop(idempotency_key, None)
        if self._redis is not None:
            await cast(Any, self._redis).hdel(OPEN_KEY, idempotency_key)
        if removed is not None:
            logger.info("exposure released", key=idempotency_key)
            return True
        return False


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()
