"""Retcode handling: transient failures retry, hard failures never do."""

from __future__ import annotations

import json

import pytest
from app.consumer import ExecutionConsumer
from app.mt5_executor import (
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID_VOLUME,
    TRADE_RETCODE_NO_MONEY,
    TRADE_RETCODE_REQUOTE,
    MT5Executor,
)
from fakes import (
    FakeExecutionDB,
    FakeMt5Module,
    FakeRedis,
    FakeSendResult,
    filled_result,
    make_order,
)
from vix_core.config import Settings


def _consumer(fake_mt5: FakeMt5Module) -> tuple[ExecutionConsumer, FakeRedis, FakeExecutionDB]:
    settings = Settings(
        service_name="execution-test",
        shadow_mode=False,
        database_url="unused://test",
    )
    db = FakeExecutionDB()
    redis_fake = FakeRedis()
    executor = MT5Executor(settings, mt5=fake_mt5, backoff_base_s=0.001)
    consumer = ExecutionConsumer(settings, db, redis_fake, executor=executor)  # type: ignore[arg-type]
    return consumer, redis_fake, db  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_requote_then_done_eventually_fills() -> None:
    fake_mt5 = FakeMt5Module(
        send_results=[
            FakeSendResult(retcode=TRADE_RETCODE_REQUOTE, comment="try again"),
            filled_result(ticket=555_001),
        ]
    )
    consumer, redis_fake, db = _consumer(fake_mt5)

    await redis_fake.xadd(
        "order.request", make_order(idempotency_key="sig-retry-0003", signal_id="c" * 32)
    )
    processed = await consumer.drain(block_ms=1)

    assert processed == 1
    assert len(fake_mt5.order_send_calls) == 2  # retried exactly once more
    assert fake_mt5.order_send_calls[-1]["symbol"] == "Volatility 75 Index"

    entries = redis_fake.entries("order.filled")
    assert len(entries) == 1  # order.filled emitted after eventual success
    body = json.loads(entries[0][1]["signal"])
    assert body["retcode"] == TRADE_RETCODE_DONE
    assert db.statuses["sig-retry-0003"] == "filled"
    assert consumer.duplicates_suppressed == 0


@pytest.mark.asyncio
async def test_hard_failure_never_retries() -> None:
    fake_mt5 = FakeMt5Module(
        send_results=[
            FakeSendResult(retcode=TRADE_RETCODE_NO_MONEY, comment="not enough"),
        ]
    )
    consumer, redis_fake, _ = _consumer(fake_mt5)

    await redis_fake.xadd(
        "order.request", make_order(idempotency_key="sig-hard-0004", signal_id="d" * 32)
    )
    await consumer.drain(block_ms=1)

    assert len(fake_mt5.order_send_calls) == 1  # NO retry on hard failure
    rejected = redis_fake.entries("order.rejected")
    assert len(rejected) == 1
    fields = rejected[0][1]
    assert "NO_MONEY" in fields["rejected_reason"]
    assert "attempts=1" in fields["detail"]


@pytest.mark.asyncio
async def test_invalid_volume_is_terminal() -> None:
    fake_mt5 = FakeMt5Module(send_results=[FakeSendResult(retcode=TRADE_RETCODE_INVALID_VOLUME)])
    consumer, redis_fake, db = _consumer(fake_mt5)

    await redis_fake.xadd(
        "order.request", make_order(idempotency_key="sig-vol-0005", signal_id="e" * 32)
    )
    await consumer.drain(block_ms=1)

    assert len(fake_mt5.order_send_calls) == 1
    assert db.statuses["sig-vol-0005"] == "rejected"
    assert redis_fake.entries("order.filled") == []


@pytest.mark.asyncio
async def test_exhausted_retries_publishes_rejection() -> None:
    fake_mt5 = FakeMt5Module(
        send_results=[
            FakeSendResult(retcode=TRADE_RETCODE_REQUOTE),
            FakeSendResult(retcode=TRADE_RETCODE_REQUOTE),
            FakeSendResult(retcode=TRADE_RETCODE_REQUOTE),
        ]
    )
    consumer, redis_fake, db = _consumer(fake_mt5)

    await redis_fake.xadd(
        "order.request", make_order(idempotency_key="sig-exh-0006", signal_id="f" * 32)
    )
    await consumer.drain(block_ms=1)

    assert len(fake_mt5.order_send_calls) == 3  # MAX_ATTEMPTS respected
    rejected = redis_fake.entries("order.rejected")
    assert len(rejected) == 1
    assert "RETRY_EXHAUSTED" in rejected[0][1]["rejected_reason"]
    assert db.statuses["sig-exh-0006"] == "rejected"
