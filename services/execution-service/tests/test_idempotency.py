"""Idempotency: duplicate order.request must execute exactly once."""

from __future__ import annotations

import json

import pytest
from app.consumer import ExecutionConsumer
from app.mt5_executor import MT5Executor
from fakes import (
    FakeExecutionDB,
    FakeMt5Module,
    FakeRedis,
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
async def test_duplicate_request_executes_once() -> None:
    fake_mt5 = FakeMt5Module(send_results=[filled_result(ticket=111_222)])
    consumer, redis_fake, db = _consumer(fake_mt5)

    payload = make_order()
    # "Concurrent" duplicate: two identical entries land before draining.
    await redis_fake.xadd("order.request", payload)
    await redis_fake.xadd("order.request", payload)

    processed = await consumer.drain(block_ms=1)
    assert processed == 2

    assert len(fake_mt5.order_send_calls) == 1  # THE assertion per spec
    assert consumer.duplicates_suppressed == 1
    assert db.statuses[payload["idempotency_key"]] == "filled"
    assert db.filled_meta[payload["idempotency_key"]]["ticket"] == 111_222

    filled_entries = redis_fake.entries("order.filled")
    assert len(filled_entries) == 1
    fields = filled_entries[0][1]
    body = json.loads(fields["signal"])
    assert body["retcode_description"].startswith("DONE")
    assert fields["ticket"] == "111222"

    # Redis fast-path now carries the terminal outcome.
    cached = await redis_fake.get("result:order:sig-test-0001")
    assert cached is not None and json.loads(cached)["accepted"] is True


@pytest.mark.asyncio
async def test_db_unique_backstop_blocks_second_execution() -> None:
    """Even with an empty Redis cache the DB constraint dedupes."""
    fake_mt5 = FakeMt5Module(send_results=[filled_result()])
    consumer, redis_fake, db = _consumer(fake_mt5)

    payload = make_order(idempotency_key="sig-dup-0002", signal_id="b" * 32)
    await redis_fake.xadd("order.request", payload)
    # Simulate a competing worker having won the insert already.
    from vix_core.schemas import OrderRequest

    await db.insert_pending(OrderRequest.model_validate(payload))

    await consumer.drain(block_ms=1)

    assert len(fake_mt5.order_send_calls) == 0
    assert consumer.duplicates_suppressed == 1
