"""Redis Stream consumer: ohlcv.update -> features -> feature.computed.

Uses a consumer group so multiple feature-service replicas can share the
load. Correlation ids are propagated end-to-end and bound to log context.
Poison messages (malformed payloads) are ACKed to a dead-letter stream;
transient processing failures stay PENDING for redelivery.
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

from .compute import FEATURE_WINDOW, snapshot_from_bars
from .db import FeatureDatabase

logger = get_logger(__name__)

STREAM_IN = "ohlcv.update"
STREAM_OUT = "feature.computed"
STREAM_DLQ = "ohlcv.update:dlq"
GROUP = "feature-service"
MAX_DELIVERIES = 3
MIN_BARS = 250  # below this the warmup contract cannot hold


class FeatureConsumer:
    """Consumer-group worker turning bar events into feature snapshots."""

    def __init__(self, settings: Settings, db: FeatureDatabase, redis: aioredis.Redis) -> None:
        self._settings = settings
        self._db = db
        self._redis = redis
        self._consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.processed = 0

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        await self._ensure_group()
        logger.info("consumer started", group=GROUP, consumer=self._consumer)
        while True:
            try:
                response = await self._redis.xreadgroup(
                    GROUP,
                    self._consumer,
                    {STREAM_IN: ">"},
                    count=16,
                    block=5_000,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("xreadgroup failed; retrying")
                await asyncio.sleep(2)
                continue

            for _stream, messages in response or []:
                for message_id, fields in messages:
                    await self._handle(message_id, fields)

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM_IN, GROUP, id="0", mkstream=True)
            logger.info("consumer group created", group=GROUP)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # ------------------------------------------------------------------

    async def _handle(self, message_id: bytes, fields: dict[bytes | str, bytes]) -> None:
        decoded = {
            (key.decode() if isinstance(key, bytes) else str(key)): (
                value.decode() if isinstance(value, bytes) else str(value)
            )
            for key, value in fields.items()
        }
        correlation_id = get_or_create_correlation_id(decoded)
        bind_correlation_id(correlation_id)

        symbol = decoded.get("symbol")
        timeframe = decoded.get("timeframe")
        if not symbol or not timeframe or "ts" not in decoded:
            logger.error(
                "malformed bar event; dead-lettering",
                message_id=str(message_id),
                fields=len(decoded),
            )
            await self._dead_letter(message_id, decoded, reason="malformed")
            unbind_correlation_id()
            return

        deliveries = await self._delivery_count(message_id)
        try:
            await self.process_bar_event(
                symbol=symbol,
                timeframe=timeframe,
                ts_str=decoded["ts"],
                correlation_id=correlation_id,
            )
        except Exception:
            if deliveries >= MAX_DELIVERIES:
                logger.exception(
                    "event failed repeatedly; dead-lettering",
                    message_id=str(message_id),
                    deliveries=deliveries,
                )
                await self._dead_letter(message_id, decoded, reason="max_deliveries")
                await self._ack(message_id)
            else:
                logger.exception("event processing failed; leaving pending")
            return

        await self._ack(message_id)
        self.processed += 1
        unbind_correlation_id()

    async def process_bar_event(
        self, *, symbol: str, timeframe: str, ts_str: str, correlation_id: str
    ) -> dict[str, object]:
        """Fetch window -> compute -> persist -> publish downstream."""
        bars = await self._db.fetch_bars(symbol, timeframe, FEATURE_WINDOW)
        if len(bars) < MIN_BARS:
            logger.warning(
                "insufficient history for features",
                symbol=symbol,
                timeframe=timeframe,
                have=len(bars),
                need=MIN_BARS,
            )
            return {}

        snapshot = snapshot_from_bars(bars, symbol, timeframe)
        await self._db.upsert_feature_row(snapshot)

        zone_ids = [str(zone.get("id")) for zone in as_zone_list(snapshot["zones"])]
        event = {
            "symbol": symbol,
            "timeframe": timeframe,
            "ts": str(snapshot["ts"]),
            "close": str(snapshot["close"]),
            "rsi": str(snapshot["rsi"]),
            "atr_norm": str(snapshot["atr_norm"]),
            "zone_ids": ",".join(zone_ids),
        }
        entry = cast(dict[Any, Any], stream_fields(dict(event), correlation_id))
        await self._redis.xadd(STREAM_OUT, entry)
        logger.info(
            "features published",
            timeframe=timeframe,
            ts=ts_str,
            zones=len(zone_ids),
        )
        return snapshot

    # ------------------------------------------------------------------

    async def _ack(self, message_id: bytes) -> None:
        await self._redis.xack(STREAM_IN, GROUP, message_id)

    async def _delivery_count(self, message_id: bytes) -> int:
        pending = await self._redis.xpending_range(
            STREAM_IN, GROUP, min=message_id, max=message_id, count=1
        )
        if not pending:
            return 1
        entry = pending[0]
        times = entry.get("times_delivered") or entry.get("time_delivered") or 1
        return int(times)

    async def _dead_letter(self, message_id: bytes, fields: dict[str, str], *, reason: str) -> None:
        payload = dict(fields)
        payload["dlq_reason"] = reason
        payload["original_id"] = message_id.decode()
        await self._redis.xadd(STREAM_DLQ, cast(dict[Any, Any], payload))
        await self._ack(message_id)
        logger.warning("message dead-lettered", reason=reason)


def as_zone_list(value: object) -> list[dict[str, object]]:
    """Zones arrive either pre-parsed (DB path) or JSON-encoded (replay)."""
    if isinstance(value, str):
        return list(json.loads(value))
    if isinstance(value, list):
        return value
    return []
