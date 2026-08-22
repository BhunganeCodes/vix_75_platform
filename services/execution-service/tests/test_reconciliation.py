"""Reconciliation: ghost local positions close from MT5 deal history.

Uses a REAL TimescaleDB container (spec requirement) plus the fake MT5
module; skipped automatically when Docker is unavailable.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

pytest.importorskip("testcontainers")

# psycopg3 async requires SelectorLoop on Windows.
if sys.platform == "win32":  # pragma: no cover - platform guard
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db import ExecutionDatabase
from app.reconciliation import Reconciler
from fakes import FakeDeal, FakeMt5Module, FakeRedis
from testcontainers.postgres import PostgresContainer
from vix_core.config import Settings

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "infra" / "timescale" / "schema.sql"
TICKET = 900_141
NO_DEAL_TICKET = 900_142


def _seed_filled_trade(dsn: str, *, key: str, ticket: int) -> None:
    signal_uuid = f"11111111-2222-3333-4444-{ticket:012d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO signals (id, symbol, ltf_timeframe, direction,
                                 entry, sl, tp1, tp2, score, max_score,
                                 components)
            VALUES (%s,'Volatility 75 Index','M15','BUY',100,95,110,115,6,7,
                    '{}'::jsonb)
            """,
            (signal_uuid,),
        )
        conn.execute(
            """
            INSERT INTO trades (idempotency_key, signal_id, symbol, side,
                                lots, entry_price, sl_price, tp_price,
                                status, broker_ticket)
            VALUES (%s, %s, 'Volatility 75 Index', 'BUY',
                    0.2, 100.0, 95.0, 110.0, 'filled', %s)
            """,
            (key, signal_uuid, ticket),
        )


@pytest.fixture(scope="module")
def timescale_dsn() -> Iterator[str]:
    try:
        pg = PostgresContainer("timescale/timescaledb:2.17.2-pg16")
        pg.start()
    except Exception as exc:
        pytest.skip(f"docker/testcontainers unavailable: {exc}")
    try:
        dsn = pg.get_connection_url().replace("+psycopg2", "")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield dsn
    finally:
        pg.stop()


class TestReconciliation:
    @pytest.mark.asyncio
    async def test_ghost_position_closed_from_history(self, timescale_dsn: str) -> None:
        fake_mt5 = FakeMt5Module()
        fake_mt5.positions = []  # position gone at the broker
        # Closing deal history for our ticket: exited at 103.0 for +60.00.
        fake_mt5.deals[TICKET] = [FakeDeal(entry=1, price=103.0, profit=60.0, volume=0.2)]
        _seed_filled_trade(timescale_dsn, key="sig-recon-ghost", ticket=TICKET)

        settings = Settings(service_name="reconciler-test", database_url=timescale_dsn)
        db = ExecutionDatabase(timescale_dsn)
        redis_fake = FakeRedis()
        await db.connect()
        reconciler = Reconciler(settings, db, redis_fake, mt5=fake_mt5)

        counters = await reconciler.run_once()
        await db.close()

        assert counters["ghosts_closed"] == 1

        closed_entry = redis_fake.entries("order.closed")
        assert len(closed_entry) == 1
        fields = closed_entry[0][1]
        assert fields["ticket"] == str(TICKET)
        assert float(fields["pnl"]) == pytest.approx(60.0)
        assert float(fields["exit_price"]) == pytest.approx(103.0)

        with psycopg.connect(timescale_dsn) as conn:
            row = conn.execute(
                "SELECT closed_at, profit FROM trades WHERE broker_ticket=%s",
                (TICKET,),
            ).fetchone()
        assert row is not None
        assert row[0] is not None  # marked closed
        assert float(row[1]) == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_orphan_logged_not_crashing(self, timescale_dsn: str) -> None:
        fake_mt5 = FakeMt5Module()
        fake_mt5.positions = [
            type(
                "P",
                (),
                {
                    "ticket": 424_242,
                    "symbol": "Volatility 75 Index",
                    "volume": 0.5,
                    "profit": -3.0,
                    "price_open": 101.0,
                    "type": 0,
                },
            )()
        ]
        settings = Settings(service_name="reconciler-test", database_url=timescale_dsn)
        db = ExecutionDatabase(timescale_dsn)
        await db.connect()
        reconciler = Reconciler(settings, db, FakeRedis(), mt5=fake_mt5)

        counters = await reconciler.run_once()  # must not raise
        await db.close()

        assert counters["orphans"] == 1
        assert counters["ghosts_closed"] == 0

    @pytest.mark.asyncio
    async def test_no_closing_deal_yet_leaves_open(self, timescale_dsn: str) -> None:
        fake_mt5 = FakeMt5Module()
        fake_mt5.positions = []
        fake_mt5.deals[NO_DEAL_TICKET] = []  # history not propagated yet
        _seed_filled_trade(timescale_dsn, key="sig-recon-nodeal", ticket=NO_DEAL_TICKET)

        settings = Settings(service_name="reconciler-test", database_url=timescale_dsn)
        db = ExecutionDatabase(timescale_dsn)
        await db.connect()
        reconciler = Reconciler(settings, db, FakeRedis(), mt5=fake_mt5)

        counters = await reconciler.run_once()
        await db.close()

        assert counters["ghosts_closed"] == 0
        with psycopg.connect(timescale_dsn) as conn:
            row = conn.execute(
                "SELECT closed_at FROM trades WHERE broker_ticket=%s",
                (NO_DEAL_TICKET,),
            ).fetchone()
        assert row is not None and row[0] is None
