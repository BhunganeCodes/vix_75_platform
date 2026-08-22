"""Risk-service consumer: signal.generated -> gate -> order.request | rejected.

Decision pipeline (first failing gate wins):

1. Schema validation of the Signal payload        -> invalid_signal
2. Exposure preflight (count / total risk)        -> max_open_trades_reached
                                                     max_total_risk_exceeded
3. Account + symbol snapshots from Redis cache    -> account_data_unavailable
4. Broker stops-level check (SL/TP distances)     -> stops_level_violation
5. Position sizing via vix_core.risk              -> risk_too_small
                                                    margin_exceeded

Approved intents are XADDed to ``order.request``; rejections to
``signal.rejected`` with the canonical RejectionReason.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass
from typing import Any, cast

import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from vix_core.config import Settings
from vix_core.correlation import (
    bind_correlation_id,
    get_or_create_correlation_id,
    stream_fields,
    unbind_correlation_id,
)
from vix_core.logging import get_logger
from vix_core.risk import SizingStatus, SymbolConstraints
from vix_core.schemas import (
    Direction,
    OrderRequest,
    RejectionReason,
    Signal,
)

from .exposure import ExposureTracker, OpenPosition, now_iso
from .sizing import size_position
from .validator import AccountSnapshot, RiskValidator

logger = get_logger(__name__)

STREAM_IN = "signal.generated"
STREAM_APPROVED = "order.request"
STREAM_REJECTED = "signal.rejected"
GROUP = "risk-service"


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of the gate chain: approval carries sized lots."""

    approved: bool
    reason: RejectionReason | None = None
    detail: str = ""
    lots: float = 0.0
    risk_amount: float = 0.0


class RiskConsumer:
    """Consumer-group worker applying hard gates before execution."""

    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        *,
        tracker: ExposureTracker | None = None,
        validator: RiskValidator | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._tracker = tracker or ExposureTracker(
            redis,
            max_open_trades=settings.max_open_positions,
            max_total_risk_pct=settings.max_total_risk_pct,
        )
        self._validator = validator or RiskValidator(redis)
        self._consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.processed = 0
        self.approved_count = 0
        self.rejected_count = 0

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        await self._ensure_group()
        logger.info("risk consumer started", consumer=self._consumer)
        while True:
            processed = await self.drain(block_ms=5_000)
            if processed == 0:
                await asyncio.sleep(self._settings.poll_interval_seconds)

    async def run_once(self, *, block_ms: int = 1_500) -> int:
        await self._ensure_group()
        return await self.drain(block_ms=block_ms)

    async def drain(self, *, block_ms: int) -> int:
        try:
            response = await self._redis.xreadgroup(
                GROUP,
                self._consumer,
                {STREAM_IN: ">"},
                count=16,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                logger.exception("xreadgroup failed")
                await asyncio.sleep(2)
            return 0
        except Exception:
            logger.exception("xreadgroup failed; retrying")
            await asyncio.sleep(2)
            return 0

        count = 0
        for _stream, messages in response or []:
            for message_id, fields in messages:
                await self._handle(message_id, fields)
                await self._ack(message_id)
                count += 1
        return count

    # ------------------------------------------------------------------

    async def _handle(self, message_id: bytes, fields: dict[bytes | str, bytes]) -> None:
        decoded = {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in fields.items()
        }
        correlation_id = get_or_create_correlation_id(decoded)
        bind_correlation_id(correlation_id)
        try:
            try:
                raw_signal = json.loads(decoded["signal"]) if "signal" in decoded else None
                signal = Signal.model_validate(raw_signal) if raw_signal else None
            except (json.JSONDecodeError, KeyError, ValueError):
                logger.exception("malformed signal payload; dropping", message=str(message_id))
                return
            if signal is None:
                logger.error("signal event missing payload", message=str(message_id))
                return

            decision = await self._decide(signal)
            if decision.approved:
                order = OrderRequest(
                    idempotency_key=signal.id,
                    signal_id=signal.id,
                    symbol=signal.symbol,
                    direction=Direction(str(signal.direction)),
                    lots=decision.lots,
                    entry=signal.entry,
                    sl=signal.sl,
                    tp=signal.tp1,
                )
                entry_fields = cast(
                    dict[Any, Any],
                    stream_fields(order.model_dump(mode="json"), correlation_id),
                )
                await self._redis.xadd(STREAM_APPROVED, entry_fields)
                await self._register_exposure(signal, order, decision.risk_amount)
                self.approved_count += 1
                logger.info(
                    "order approved",
                    signal_id=signal.id,
                    direction=str(signal.direction),
                    lots=order.lots,
                )
            else:
                assert decision.reason is not None  # noqa: S101 - invariant
                fields_out = cast(
                    dict[Any, Any],
                    stream_fields(
                        {
                            "signal_id": signal.id,
                            "symbol": signal.symbol,
                            "direction": str(signal.direction),
                            "rejected_reason": decision.reason.value,
                            "detail": decision.detail[:300],
                        },
                        correlation_id,
                    ),
                )
                await self._redis.xadd(STREAM_REJECTED, fields_out)
                self.rejected_count += 1
                logger.warning(
                    "signal rejected",
                    signal_id=signal.id,
                    rejected_reason=decision.reason.value,
                    detail=decision.detail,
                )
        finally:
            self.processed += 1
            unbind_correlation_id()

    # ------------------------------------------------------------------

    async def _decide(self, signal: Signal) -> Decision:
        # ---- Gate 2: exposure -----------------------------------------
        balance_hint = await self._balance_hint()
        ok, exposure_reason = await self._tracker.preflight(balance=balance_hint)
        if not ok:
            assert exposure_reason is not None  # noqa: S101 - invariant
            return Decision(False, exposure_reason, "exposure limits reached")

        # ---- Gate 3: cached market/account context (fail closed) -------
        account = await self._validator.load_account()
        constraints = await self._validator.load_symbol_constraints(signal.symbol)
        if account is None or constraints is None:
            return Decision(
                False,
                RejectionReason.ACCOUNT_DATA_UNAVAILABLE,
                "mt5:account_info / symbol spec missing from Redis cache",
            )

        # ---- Gate 4: broker stops level ---------------------------------
        stops_detail = self._validator.check_stops(signal, constraints)
        if stops_detail is not None:
            return Decision(False, RejectionReason.STOPS_LEVEL_VIOLATION, stops_detail)

        # ---- Gate 5: sizing (clamp-DOWN semantics live in vix_core) -----
        result = size_position(
            signal,
            account=account,
            constraints=constraints,
            risk_pct=self._settings.risk_pct_per_trade,
            margin_usage_cap=self._settings.margin_usage_cap,
        )
        if result.status in (SizingStatus.OK, SizingStatus.CAPPED_BY_VOLUME_MAX):
            return Decision(
                approved=True,
                lots=result.lots,
                risk_amount=_risk_amount(signal, constraints, result.lots),
            )
        mapping = {
            SizingStatus.REJECT_RISK_TOO_SMALL: RejectionReason.RISK_TOO_SMALL,
            SizingStatus.REJECT_MARGIN: RejectionReason.MARGIN_EXCEEDED,
            SizingStatus.REJECT_STOPS_LEVEL: RejectionReason.STOPS_LEVEL_VIOLATION,
            SizingStatus.REJECT_INVALID_INPUT: RejectionReason.INVALID_SIGNAL,
        }
        reason = mapping.get(result.status, RejectionReason.INVALID_SIGNAL)
        return Decision(False, reason, result.reason)

    # ------------------------------------------------------------------

    async def _register_exposure(
        self, signal: Signal, order: OrderRequest, risk_amount: float
    ) -> None:
        position = OpenPosition(
            idempotency_key=order.idempotency_key,
            signal_id=signal.id,
            symbol=order.symbol,
            direction=order.direction,
            lots=order.lots,
            entry=order.entry,
            sl=order.sl,
            risk_amount=risk_amount,
            opened_ts=now_iso(),
        )
        await self._tracker.register(position)

    async def _balance_hint(self) -> float:
        account: AccountSnapshot | None = await self._validator.load_account()
        return account.balance if account else float("inf")

    # ------------------------------------------------------------------

    async def _ack(self, message_id: bytes) -> None:
        await self._redis.xack(STREAM_IN, GROUP, message_id)

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM_IN, GROUP, id="0", mkstream=True)
            logger.info("consumer group created", group=GROUP)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise


def _risk_amount(signal: Signal, constraints: SymbolConstraints, lots: float) -> float:
    """Account-currency loss if SL is hit at the sized volume."""
    sl_distance = abs(signal.entry - signal.sl)
    loss_per_lot = (sl_distance / constraints.tick_size) * constraints.tick_value
    return round(loss_per_lot * lots, 2)
