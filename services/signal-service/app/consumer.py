"""Signal-service consumer: feature.computed -> confluence -> signal.generated.

Pipeline per LTF event:

1. Parse the bar-close event and load the stored feature row (full
   indicators + zone snapshot) from TimescaleDB.
2. Fetch the HMM regime and LightGBM meta-label snapshots from Redis
   (written by ml-service). Missing data fails closed.
3. Run the pure engine; on a fired signal, persist to the ``signals``
   table and XADD ``signal.generated`` with the correlation id.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
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
from vix_core.schemas import RegimeState

from .db import SignalDatabase
from .engine import MarketContext, SignalEngine

logger = get_logger(__name__)

STREAM_IN = "feature.computed"
STREAM_OUT = "signal.generated"
GROUP = "signal-service"


class SignalConsumer:
    """Consumer-group worker turning feature events into gated signals."""

    def __init__(
        self,
        settings: Settings,
        db: SignalDatabase,
        redis: aioredis.Redis,
        *,
        engine: SignalEngine | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._redis = redis
        self._consumer = f"{socket.gethostname()}-{os.getpid()}"
        if engine is None:
            from .engine import SignalEngine

            engine = SignalEngine(
                min_confluence=settings.confluence_min,
                min_p_win=settings.min_p_win,
                allow_s0_fade=settings.allow_s0_fade,
                sl_atr_buffer=settings.sl_atr_buffer,
                rr_tp_1=settings.tp_rr_1,
                rr_tp_2=settings.tp_rr_2,
            )
        self._engine = engine
        self.processed = 0
        self.signals_fired = 0

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        await self._ensure_group()
        logger.info("signal consumer started", consumer=self._consumer)
        while True:
            processed = await self.drain(block_ms=5_000)
            if processed == 0:
                await asyncio.sleep(self._settings.poll_interval_seconds)

    async def run_once(self, *, block_ms: int = 1_500) -> int:
        """Process whatever is pending right now; used by tests/CLI."""
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
            symbol = decoded.get("symbol", "")
            timeframe = decoded.get("timeframe", "")
            if timeframe not in self._settings.ltf_timeframes:
                return  # HTF/MTF rows only feed context, not evaluation

            market = await self._market_context(symbol)
            ltf = await self._db.fetch_ltf_row(symbol, timeframe)
            if ltf is None:
                logger.warning("feature row missing; skipping", symbol=symbol, timeframe=timeframe)
                return

            evaluation = await asyncio.to_thread(self._engine.evaluate, ltf, market)
            if evaluation.signal is None:
                logger.info(
                    "confluence rejected",
                    timeframe=timeframe,
                    score=evaluation.score,
                    rejections=list(evaluation.rejections),
                )
                return

            signal = evaluation.signal
            await self._db.insert_signal(signal)
            # Single JSON blob under one key keeps the contract explicit
            # for downstream consumers (risk-service parses it verbatim).
            entry = stream_fields(
                {"signal": json.dumps(signal.model_dump(mode="json"))},
                correlation_id,
            )
            entry_typed = cast("dict[Any, Any]", entry)
            await self._redis.xadd(STREAM_OUT, entry_typed)
            self.processed += 1
            self.signals_fired += 1
            logger.info(
                "signal generated",
                direction=str(signal.direction),
                timeframe=timeframe,
                score=evaluation.score,
            )
        finally:
            unbind_correlation_id()

    # ------------------------------------------------------------------

    async def _market_context(self, symbol: str) -> MarketContext:
        from app.engine import MarketContext

        regime_raw = await self._redis.get("regime:current")
        meta_raw = await self._redis.get("meta_label:current")

        regime = "unknown"
        if isinstance(regime_raw, (bytes, str)):
            data = json.loads(regime_raw)
            candidate = str(data.get("regime", ""))
            try:
                regime = RegimeState(candidate).value
            except ValueError:
                regime = "unknown"

        p_win: float | None = None
        if isinstance(meta_raw, (bytes, str)):
            meta = json.loads(meta_raw)
            p_up = meta.get("p_up")
            p_down = meta.get("p_down")
            if isinstance(p_up, (int, float)) and isinstance(p_down, (int, float)):
                p_win = float(p_up) / max(float(p_up) + float(p_down), 1e-9)

        htf_trend = await self._db.fetch_htf_trend(symbol, tuple(self._settings.htf_timeframes))
        return MarketContext(htf_trend=htf_trend, regime=regime, p_win=p_win)

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
