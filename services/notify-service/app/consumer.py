"""Notify-service consumer: lifecycle logging + Telegram alerts.

Subscribes to the full signal lifecycle via ONE consumer group reading
five streams simultaneously:

    signal.generated -> 🟡 telegram + audit
    signal.rejected  -> audit always; telegram only when
                        settings.alert_rejections is true (muted default)
    order.filled     -> 🟢 telegram + audit
    order.rejected   -> 🔴 telegram + audit
    order.closed     -> 🔵 telegram + audit

Messages are ACKed after processing regardless of Telegram delivery -
alerts are best-effort, the audit trail is the source of truth.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket

import httpx
import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from vix_core.config import Settings
from vix_core.correlation import (
    bind_correlation_id,
    get_or_create_correlation_id,
    unbind_correlation_id,
)
from vix_core.logging import get_logger

from .lifecycle import LifecycleLogger
from .telegram import TelegramSender

logger = get_logger(__name__)

GROUP = "notify-service"
STREAMS: tuple[str, ...] = (
    "signal.generated",
    "signal.rejected",
    "order.filled",
    "order.rejected",
    "order.closed",
)


class NotifyConsumer:
    """Fan-in worker across all lifecycle streams."""

    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        lifecycle: LifecycleLogger,
        sender: TelegramSender | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._lifecycle = lifecycle
        self._sender = sender or TelegramSender(
            settings,
            httpx.AsyncClient(base_url="https://api.telegram.org", timeout=10.0),
        )
        self._consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.processed = 0
        self.alerts_sent = 0

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        await self._ensure_groups()
        logger.info("notify consumer started", consumer=self._consumer)
        while True:
            processed = await self.drain(block_ms=5_000)
            if processed == 0:
                await asyncio.sleep(self._settings.poll_interval_seconds)

    async def run_once(self, *, block_ms: int = 1_500) -> int:
        await self._ensure_groups()
        return await self.drain(block_ms=block_ms)

    async def drain(self, *, block_ms: int) -> int:
        stream_map: dict[str, str] = dict.fromkeys(STREAMS, ">")
        try:
            response = await self._redis.xreadgroup(
                GROUP,
                self._consumer,
                stream_map,  # type: ignore[arg-type]
                count=16,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                logger.warning("consumer groups missing; recreating")
                await self._ensure_groups()
                return 0
            logger.exception("xreadgroup failed")
            await asyncio.sleep(2)
            return 0
        except Exception:
            logger.exception("xreadgroup failed; retrying")
            await asyncio.sleep(2)
            return 0

        count = 0
        for stream, messages in response or []:
            for message_id, fields in messages:
                await self._handle(str(stream), message_id, fields)
                await self._ack(stream, message_id)
                count += 1
        return count

    # ------------------------------------------------------------------

    def _decode(self, fields: dict[bytes | str, bytes]) -> dict[str, str]:
        return {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in fields.items()
        }

    async def _handle(
        self, stream: str, message_id: bytes, fields: dict[bytes | str, bytes]
    ) -> None:
        decoded = self._decode(fields)
        correlation_id = get_or_create_correlation_id(decoded)
        bind_correlation_id(correlation_id)
        try:
            subject = str(
                decoded.get("signal_id") or decoded.get("idempotency_key") or message_id.decode()
            )

            if stream == "signal.generated":
                signal_payload: dict[str, object] | None = None
                blob = decoded.get("signal")
                if blob:
                    try:
                        parsed = json.loads(blob)
                        signal_payload = {
                            **decoded,
                            "direction": parsed.get("direction"),
                            "symbol": parsed.get("symbol"),
                            "entry": parsed.get("entry"),
                            "sl": parsed.get("sl"),
                            "tp1": parsed.get("tp1"),
                            "tp2": parsed.get("tp2"),
                            "score": parsed.get("score"),
                            "max_score": parsed.get("max_score"),
                        }
                    except json.JSONDecodeError:
                        signal_payload = dict(decoded)
                text = self._sender.format_generated(signal_payload or dict(decoded))
                await self._lifecycle.record(
                    event=stream,
                    subject=subject,
                    payload={"correlation_id": correlation_id},
                )
                if await self._sender.send(text):
                    self.alerts_sent += 1

            elif stream == "signal.rejected":
                await self._lifecycle.record(
                    event=stream,
                    subject=subject,
                    outcome="rejected",
                    payload={"rejected_reason": decoded.get("rejected_reason")},
                )
                if self._settings.alert_rejections:
                    text = self._sender.format_rejected(dict(decoded))
                    if await self._sender.send(text):
                        self.alerts_sent += 1
                else:
                    logger.debug(
                        "rejection muted",
                        rejected_reason=decoded.get("rejected_reason"),
                    )

            elif stream == "order.filled":
                await self._lifecycle.record(
                    event=stream,
                    subject=str(decoded.get("idempotency_key", subject)),
                    payload={"ticket": decoded.get("ticket")},
                )
                text = self._sender.format_filled(dict(decoded))
                if await self._sender.send(text):
                    self.alerts_sent += 1

            elif stream == "order.rejected":
                await self._lifecycle.record(
                    event=stream,
                    subject=str(decoded.get("idempotency_key", subject)),
                    outcome="error",
                    payload={
                        "rejected_reason": decoded.get("rejected_reason"),
                        "detail": decoded.get("detail"),
                    },
                )
                text = self._sender.format_rejected(dict(decoded))
                if await self._sender.send(text):
                    self.alerts_sent += 1

            elif stream == "order.closed":
                await self._lifecycle.record(
                    event=stream,
                    subject=str(decoded.get("idempotency_key", subject)),
                    payload={
                        "pnl": decoded.get("pnl"),
                        "ticket": decoded.get("ticket"),
                    },
                )
                text = self._sender.format_closed(dict(decoded))
                if await self._sender.send(text):
                    self.alerts_sent += 1

            else:  # pragma: no cover - defensive
                logger.warning("unknown lifecycle stream", stream=stream)

            self.processed += 1
        finally:
            unbind_correlation_id()

    # ------------------------------------------------------------------

    async def _ack(self, stream: str, message_id: bytes) -> None:
        await self._redis.xack(stream, GROUP, message_id)

    async def _ensure_groups(self) -> None:
        for stream in STREAMS:
            try:
                await self._redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
                logger.info("consumer group created", group=GROUP, stream=stream)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
