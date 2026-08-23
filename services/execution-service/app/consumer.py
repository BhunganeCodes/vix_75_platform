"""Execution-service consumer: order.request -> MT5 -> order.filled/rejected.

Idempotency (spec requirement):

* Redis fast-path: ``result:order:<key>`` holds the terminal outcome; a
  duplicate request is ACKed without touching the broker.
* Authoritative backstop: ``trades.idempotency_key`` UNIQUE constraint -
  concurrent inserts collapse to one row and the loser skips.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from typing import Any, cast

import redis.asyncio as aioredis
from prometheus_client import Counter
from redis.exceptions import ResponseError
from vix_core.config import Settings
from vix_core.correlation import (
    bind_correlation_id,
    get_or_create_correlation_id,
    stream_fields,
    unbind_correlation_id,
)
from vix_core.logging import get_logger
from vix_core.schemas import OrderRequest

from .db import ExecutionDatabase, dumps
from .mt5_executor import MT5Executor

logger = get_logger(__name__)

STREAM_IN = "order.request"
STREAM_FILLED = "order.filled"
STREAM_REJECTED = "order.rejected"
GROUP = "execution-service"
RESULT_KEY_TEMPLATE = "result:order:{key}"
RESULT_TTL_S = 7 * 24 * 3600

ORDERS_FILLED = Counter(
    "vix75_orders_filled_total",
    "Broker-confirmed order fills",
    ["symbol"],
)
ORDERS_REJECTED = Counter(
    "vix75_orders_rejected_total",
    "Orders rejected by MT5 (hard failure or exhausted retries)",
    ["symbol", "reason"],
)


class ExecutionConsumer:
    """Consumer-group worker placing broker orders exactly once."""

    def __init__(
        self,
        settings: Settings,
        db: ExecutionDatabase,
        redis: aioredis.Redis,
        executor: MT5Executor | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._redis = redis
        self._executor = executor or MT5Executor(settings)
        self._consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.processed = 0
        self.duplicates_suppressed = 0

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        await self._ensure_group()
        logger.info("execution consumer started", consumer=self._consumer)
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
                count=8,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                # Consumer groups do not survive a Redis restart; recreate.
                logger.warning("consumer group missing; recreating")
                await self._ensure_group()
                return 0
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

    def _parse_order(self, decoded: dict[str, str]) -> OrderRequest | None:
        """Accept either a nested JSON blob or flattened model fields."""
        blob = decoded.get("order")
        data: dict[str, Any] | None = None
        if blob is not None:
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                return None
        else:
            keys = (
                "idempotency_key",
                "signal_id",
                "symbol",
                "direction",
                "lots",
                "entry",
                "sl",
                "tp",
            )
            if all(k in decoded for k in keys):
                data = {
                    **{k: decoded[k] for k in keys},
                    "deviation_points": int(decoded.get("deviation_points", 30)),
                }
        if not data:
            return None
        try:
            return OrderRequest.model_validate(data)
        except ValueError:
            return None

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
            order = self._parse_order(decoded)
            if order is None:
                logger.error("malformed order request; dropping", message=str(message_id))
                return

            # ---- Idempotency fast-path (Redis) --------------------------
            result_key = RESULT_KEY_TEMPLATE.format(key=order.idempotency_key)
            cached = await self._redis.get(result_key)
            if cached is not None:
                self.duplicates_suppressed += 1
                logger.warning(
                    "duplicate order suppressed via redis",
                    key=order.idempotency_key,
                    prior=json.loads(cached if isinstance(cached, str) else cached.decode()).get(
                        "retcode_description"
                    ),
                )
                return

            # ---- Authoritative dedupe (DB unique constraint) -------------
            inserted = await self._db.insert_pending(order)
            if not inserted:
                self.duplicates_suppressed += 1
                logger.warning("duplicate order suppressed via db", key=order.idempotency_key)
                return

            outcome = await asyncio.to_thread(
                self._executor.execute, order, correlation_id=correlation_id
            )
            res = outcome.result

            if res.accepted:
                await self._db.set_status(
                    "filled",
                    order.idempotency_key,
                    retcode=res.retcode,
                    desc=res.retcode_description,
                    ticket=res.ticket,
                    price=res.price,
                    extra={
                        "attempts": outcome.attempts,
                        "mode": "dry_run" if self._settings.dry_run_mode else "live",
                    },
                )
                filled_fields = cast(
                    dict[Any, Any],
                    stream_fields(
                        {
                            "signal": json.dumps(
                                {**order.model_dump(mode="json"), **_result_view(res)}
                            ),
                            "idempotency_key": order.idempotency_key,
                            "signal_id": order.signal_id,
                            "symbol": order.symbol,
                            "direction": str(order.direction),
                            "lots": order.lots,
                            "entry": order.entry,
                            "sl": order.sl,
                            "tp": order.tp,
                            "ticket": res.ticket or 0,
                            "fill_price": res.price or 0.0,
                        },
                        correlation_id,
                    ),
                )
                ORDERS_FILLED.labels(symbol=order.symbol).inc()
                await self._redis.xadd(STREAM_FILLED, filled_fields)
                logger.info(
                    "order lifecycle complete",
                    key=order.idempotency_key,
                    ticket=res.ticket,
                )
            else:
                await self._db.set_status(
                    "rejected",
                    order.idempotency_key,
                    retcode=res.retcode,
                    desc=res.retcode_description,
                    extra={
                        "attempts": outcome.attempts,
                        "mt5_last_error": outcome.last_error,
                        "mode": "dry_run" if self._settings.dry_run_mode else "live",
                    },
                )
                rejected_fields = cast(
                    dict[Any, Any],
                    stream_fields(
                        {
                            "signal_id": order.signal_id,
                            "idempotency_key": order.idempotency_key,
                            "symbol": order.symbol,
                            "direction": str(order.direction),
                            "rejected_reason": (res.retcode_description or "UNKNOWN"),
                            "detail": (
                                f"attempts={outcome.attempts}" f" last_error={outcome.last_error}"
                            ),
                        },
                        correlation_id,
                    ),
                )
                ORDERS_REJECTED.labels(
                    symbol=order.symbol,
                    reason=(res.retcode_description or "UNKNOWN"),
                ).inc()
                await self._redis.xadd(STREAM_REJECTED, rejected_fields)

            await self._redis.set(
                result_key,
                dumps(_result_view(res)),
                ex=RESULT_TTL_S,
            )
            await self._db.audit(
                "execution-service",
                "order.executed" if res.accepted else "order.failed",
                order.idempotency_key,
                "ok" if res.accepted else "error",
                _result_view(res),
                error=None if res.accepted else outcome.last_error,
            )
            self.processed += 1
        finally:
            unbind_correlation_id()

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


def _result_view(res: Any) -> dict[str, object]:
    return {
        "accepted": bool(res.accepted),
        "retcode": res.retcode,
        "retcode_description": res.retcode_description,
        "ticket": res.ticket,
        "price": res.price,
    }
