"""Ingest pipeline tests: mocked MT5 + optional testcontainers Postgres.

The unit suite runs everywhere with fakes. The integration suite boots a
real TimescaleDB container and is skipped automatically when Docker is not
available (e.g. CI runners without docker).
"""

from __future__ import annotations

import asyncio

# psycopg3 async cannot run on Windows' default Proactor loop.
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from app.db import Database
from app.ingest import OHLCV_STREAM, Ingestor, IngestStats
from vix_core.config import Settings
from vix_core.schemas import Bar

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _bars(start: datetime, n: int, base_price: float = 100.0) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            ts=start + timedelta(minutes=5 * i),
            open=base_price,
            high=base_price + 1,
            low=base_price - 1,
            close=base_price + 0.5 * (i % 3),
            tick_volume=1_000 + i,
        )
        for i in range(n)
    )


class FakeMT5Client:
    """Duck-typed MT5Client: deterministic bars, no terminal required."""

    def __init__(self, bars: tuple[Bar, ...]) -> None:
        self._bars = bars
        self._connected = True
        self.connect_calls = 0

    def connect(self) -> bool:
        self.connect_calls += 1
        return True

    def copy_bars(self, symbol: str, timeframe: str, count: int) -> tuple[Bar, ...]:
        return self._bars[:count]


class RecordingDB:
    def __init__(self) -> None:
        self.written: list[tuple[str, str, int]] = []

    async def upsert_bars(self, symbol: str, timeframe: str, bars: Any) -> int:
        self.written.append((symbol, timeframe, len(bars)))
        return len(bars)

    async def audit(self, *args: object, **kwargs: object) -> None:
        pass


class FakePipeline:
    def __init__(self, entries: list[dict[str, str]]) -> None:
        self._entries = entries

    def xadd(self, stream: str, fields: dict[str, str]) -> None:
        self._entries.append({"stream": stream, **fields})

    async def execute(self) -> list[object]:
        return []


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []
        self.sets: dict[str, str] = {}

    def pipeline(self, transaction: bool = False) -> FakePipeline:
        return FakePipeline(self.entries)

    async def xadd(self, stream: str, fields: dict[str, str]) -> None:
        self.entries.append({"stream": stream, **fields})


def _ingestor(bars: tuple[Bar, ...]) -> tuple[Ingestor, RecordingDB, FakeRedis]:
    settings = Settings(service_name="data-service-test")
    db = RecordingDB()
    redis_fake = FakeRedis()
    ingestor = Ingestor(
        settings=settings,
        db=db,  # type: ignore[arg-type]
        redis=redis_fake,  # type: ignore[arg-type]
        client=FakeMT5Client(bars),  # type: ignore[arg-type]
        timeframes=("M5",),
        history_bars=300,
    )
    return ingestor, db, redis_fake


class TestIngestTransform:
    def test_cycle_writes_and_publishes(self) -> None:
        start = datetime.now(tz=UTC)
        bars = _bars(start, 10)
        ingestor, db, redis_fake = _ingestor(bars)

        asyncio.run(ingestor.cycle("Volatility 75 Index"))

        assert db.written == [("Volatility 75 Index", "M5", 10)]
        assert len(redis_fake.entries) == 10
        first = redis_fake.entries[0]
        assert first["stream"] == OHLCV_STREAM
        assert first["symbol"] == "Volatility 75 Index"
        assert first["timeframe"] == "M5"
        assert "correlation_id" in first
        assert float(first["close"]) == pytest.approx(bars[0].close)
        assert ingestor.stats.bars_written == 10
        assert ingestor.stats.events_published == 10

    def test_no_republish_on_unchanged_poll(self) -> None:
        start = datetime.now(tz=UTC)
        bars = _bars(start, 5)
        ingestor, db, redis_fake = _ingestor(bars)

        asyncio.run(ingestor.cycle())
        asyncio.run(ingestor.cycle())

        # Second poll saw no new closes: nothing re-written or re-published.
        assert db.written == [("Volatility 75 Index", "M5", 5)]
        assert len(redis_fake.entries) == 5

    def test_only_new_bars_forwarded(self) -> None:
        start = datetime.now(tz=UTC)
        old = _bars(start, 5)
        new = old + _bars(start + timedelta(minutes=25), 2, base_price=101.0)
        ingestor, _, redis_fake = _ingestor(old)

        asyncio.run(ingestor.cycle())
        ingestor.client._bars = new  # type: ignore[attr-defined]
        asyncio.run(ingestor.cycle())

        assert ingestor.stats.events_published == 7
        assert redis_fake.entries[-1]["ts"] == new[-1].ts.isoformat()

    def test_filter_new_empty_window(self) -> None:
        ingestor, _, _ = _ingestor(())
        assert ingestor._filter_new("M5", ()) == ()
        assert ingestor.stats.cycles == 0


# ---------------------------------------------------------------------------
# Integration (testcontainers; skipped without Docker)
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "infra" / "timescale" / "schema.sql"


@pytest.fixture(scope="module")
def timescale_dsn() -> Iterator[str]:
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    try:
        container = PostgresContainer("timescale/timescaledb:2.17.2-pg16")
        container.start()
    except Exception as exc:
        pytest.skip(f"docker/testcontainers unavailable: {exc}")
    try:
        dsn = container.get_connection_url().replace("+psycopg2", "")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
        yield dsn
    finally:
        container.stop()


@pytest.mark.integration
class TestIngestToTimescale:
    def test_upsert_is_idempotent(self, timescale_dsn: str) -> None:
        async def scenario() -> tuple[int, int]:
            db = Database(timescale_dsn)
            await db.connect()
            bars = _bars(datetime.now(tz=UTC), 25)
            first = await db.upsert_bars("VIX75-TEST", "M5", bars)
            second = await db.upsert_bars("VIX75-TEST", "M5", bars)
            ping = await db.ping()
            await db.close()
            return first, second if ping else (-1, -2)

        first, second = asyncio.run(scenario())
        assert first == 25
        assert second == 25  # attempted...

        with psycopg.connect(timescale_dsn) as conn:
            row = conn.execute("SELECT count(*) FROM ohlcv WHERE symbol = 'VIX75-TEST'").fetchone()
        assert row is not None and row[0] == 25  # ...but stored exactly once

    def test_ping_false_when_down(self) -> None:
        async def scenario() -> bool:
            db = Database("postgresql://nobody:nope@127.0.0.1:9/void")
            try:
                return await db.ping()
            finally:
                await db.close()

        assert asyncio.run(scenario()) is False


_ = IngestStats  # re-exported for typing consumers
