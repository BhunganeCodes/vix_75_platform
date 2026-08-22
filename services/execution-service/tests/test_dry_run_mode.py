"""Dry-run mode: simulated fills flow the FULL pipeline, broker untouched.

Spec assertions:

* ``mt5.order_send`` is NEVER called when ``dry_run_mode`` is true.
* An ``order.filled`` event IS emitted with a mock (epoch-based) ticket.
* The trades row lands as 'filled' and Telegram/notify consumers see a
  normal-looking fill (proven here by the stream payload shape).
"""

from __future__ import annotations

import json
import time

import pytest
from app.consumer import ExecutionConsumer
from app.mt5_executor import TRADE_RETCODE_DONE, MT5Executor
from fakes import FakeExecutionDB, FakeMt5Module, FakeRedis, make_order
from vix_core.config import Settings
from vix_core.schemas import OrderRequest


def _dry_consumer(
    fake_mt5: FakeMt5Module,
) -> tuple[ExecutionConsumer, FakeRedis, FakeExecutionDB]:
    settings = Settings(
        service_name="execution-dryrun-test",
        shadow_mode=False,
        dry_run_mode=True,  # THE flag under test
        database_url="unused://test",
    )
    db = FakeExecutionDB()
    redis_fake = FakeRedis()
    executor = MT5Executor(settings, mt5=fake_mt5, backoff_base_s=0.001)
    consumer = ExecutionConsumer(settings, db, redis_fake, executor=executor)  # type: ignore[arg-type]
    return consumer, redis_fake, db  # type: ignore[return-value]


class TestDryRunMode:
    @pytest.mark.asyncio
    async def test_order_send_never_called_but_filled_emitted(self) -> None:
        fake_mt5 = FakeMt5Module(send_results=[])  # any real send would explode
        consumer, redis_fake, db = _dry_consumer(fake_mt5)
        before = time.time()

        await redis_fake.xadd(
            "order.request",
            make_order(idempotency_key="sig-dry-0001", signal_id="1" * 32),
        )
        processed = await consumer.drain(block_ms=1)

        assert processed == 1
        assert len(fake_mt5.order_send_calls) == 0  # THE assertion per spec

        filled = redis_fake.entries("order.filled")
        assert len(filled) == 1
        fields = filled[0][1]

        body = json.loads(fields["signal"])
        assert body["accepted"] is True
        assert body["retcode"] == TRADE_RETCODE_DONE
        assert "dry_run" in body["retcode_description"]

        # Mock ticket is epoch seconds at simulation time.
        ticket = int(fields["ticket"])
        assert before - 5 <= ticket <= time.time() + 5

        # Fill price comes from the live tick feed (fake bid/ask defaults).
        assert float(fields["fill_price"]) == pytest.approx(100.10)  # BUY -> ask (FakeTick.ask)

        # DB row is 'filled' so reconciliation/notify see a normal trade.
        assert db.statuses["sig-dry-0001"] == "filled"
        assert db.filled_meta["sig-dry-0001"]["ticket"] == ticket

        # Idempotency still applies to simulated orders.
        cached = await redis_fake.get("result:order:sig-dry-0001")
        assert cached is not None and json.loads(cached)["accepted"] is True

    @pytest.mark.asyncio
    async def test_sell_side_uses_bid_price(self) -> None:
        fake_mt5 = FakeMt5Module()
        consumer, redis_fake, _ = _dry_consumer(fake_mt5)

        await redis_fake.xadd(
            "order.request",
            make_order(
                idempotency_key="sig-dry-0002",
                signal_id="2" * 32,
                direction="SELL",
            ),
        )
        await consumer.drain(block_ms=1)

        fields = redis_fake.entries("order.filled")[0][1]
        assert float(fields["fill_price"]) == pytest.approx(100.00)  # SELL -> bid (FakeTick.bid)
        assert fake_mt5.order_send_calls == []

    @pytest.mark.asyncio
    async def test_executor_outcome_direct(self) -> None:
        settings = Settings(service_name="t", dry_run_mode=True, database_url="x://")
        executor = MT5Executor(settings, mt5=FakeMt5Module())
        outcome = executor.execute(OrderRequest.model_validate(make_order()))

        assert outcome.result.accepted is True
        assert outcome.attempts == 0  # zero broker attempts by definition
        assert outcome.result.ticket == int(time.time())  # within a second

    def test_live_mode_still_sends(self) -> None:
        """Guard: flipping the flag off restores real order_send usage."""
        settings = Settings(
            service_name="t", dry_run_mode=False, shadow_mode=False, database_url="x://"
        )
        fake_mt5 = FakeMt5Module(
            send_results=[__import__("fakes").FakeSendResult(retcode=10009, order=1, price=100.1)]
        )
        MT5Executor(settings, mt5=fake_mt5).execute(OrderRequest.model_validate(make_order()))
        assert len(fake_mt5.order_send_calls) == 1
