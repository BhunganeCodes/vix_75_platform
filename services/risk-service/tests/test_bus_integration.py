"""Full-bus integration test (testcontainers: TimescaleDB + Redis).

Flow under test, exactly as spec'd:

    XADD feature.computed
      -> signal-service consumer (subprocess, real code)
      -> asserts signal.generated entry
    -> risk-service consumer (subprocess, real code)
      -> asserts order.request entry (and NO signal.rejected)

Each stage runs as `python -m app.consumer_once` in its own process with
DATABASE_URL/REDIS_URL pointed at the containers - this isolates the two
services' `app` packages and exercises the real wiring end-to-end.

Requires Docker; skipped automatically when unavailable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("testcontainers")
pytest.importorskip("redis")

import redis as redis_sync
from testcontainers.redis import RedisContainer

SERVICE_ROOTS = {
    "signal": Path(__file__).resolve().parents[2] / "signal-service",
    "risk": Path(__file__).resolve().parents[1],
}
SCHEMA_PATH = SERVICE_ROOTS["risk"].parents[1] / "infra" / "timescale" / "schema.sql"

SYMBOL = "Volatility 75 Index"
BAR_TS = datetime(2026, 3, 3, 13, 45, tzinfo=UTC)


@pytest.fixture(scope="module")
def infra() -> dict[str, str]:
    from testcontainers.postgres import PostgresContainer

    try:
        pg = PostgresContainer("timescale/timescaledb:2.17.2-pg16")
        pg.start()
        rd = RedisContainer()
        rd.start()
    except Exception as exc:
        pytest.skip(f"docker/testcontainers unavailable: {exc}")

    try:
        dsn = pg.get_connection_url().replace("+psycopg2", "")
        redis_url = f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/0"
        with __import__("psycopg").connect(dsn, autocommit=True) as conn:
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        _seed_postgres(dsn)
        client = redis_sync.from_url(redis_url, decode_responses=True)
        _seed_redis(client)
        client.close()
        yield {"database_url": dsn, "redis_url": redis_url}
    finally:
        pg.stop()
        rd.stop()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

_DEMAND_ZONE = {
    "kind": "demand",
    "top": 100.5,
    "bottom": 99.0,
    "created_ts": "2026-03-01T00:00:00+00:00",
    "state": "fresh",
    "touches": 0,
    "score": 2,
}


def _seed_postgres(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO features (symbol, timeframe, ts, close, atr, rsi,
                                  ema50, ema200, bb_mid, stoch_k, zones)
            VALUES (%s, 'M5', %s, 100.0, 1.0, 55.0, 99.5, 98.0, 100.5, 45.0, %s)
            """,
            (SYMBOL, BAR_TS, json.dumps([_DEMAND_ZONE])),
        )
        # HTF context: H4 ema50 above ema200 => macro uptrend.
        conn.execute(
            """
            INSERT INTO features (symbol, timeframe, ts, close, atr, rsi,
                                  ema50, ema200, zones)
            VALUES (%s, 'H4', %s, 104.0, 2.0, 58.0, 103.0, 96.0, NULL)
            """,
            (SYMBOL, BAR_TS - timedelta(hours=4)),
        )
        # LTF M15 variant used by the rejection-path test.
        conn.execute(
            """
            INSERT INTO features (symbol, timeframe, ts, close, atr, rsi,
                                  ema50, ema200, zones)
            VALUES (%s, 'M15', %s, 100.2, 0.9, 56.0, 100.8, 99.0, %s)
            """,
            (
                SYMBOL,
                BAR_TS + timedelta(minutes=15),
                json.dumps([{**_DEMAND_ZONE, "top": 100.6}]),
            ),
        )


def _seed_redis(client: redis_sync.Redis) -> None:
    client.set(
        "regime:current",
        json.dumps({"regime": "S1_trend_up", "state": 1}),
    )
    client.set(
        "meta_label:current",
        json.dumps({"p_up": 0.80, "p_down": 0.05}),
    )
    client.set(
        f"mt5:symbol:{SYMBOL}",
        json.dumps(
            {
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "tick_size": 0.01,
                "tick_value": 1.0,
                "point": 0.01,
                "stops_level_points": 10,
                "margin_per_lot": 100.0,
            }
        ),
    )
    client.set(
        "mt5:account_info",
        json.dumps({"balance": 10_000.0, "equity": 10_000.0, "margin_free": 8_000.0}),
    )


def _run_consumer(stage: str, infra_env: dict[str, str]) -> None:
    root = SERVICE_ROOTS[stage]
    env = {
        **os.environ,
        **infra_env,
        "VIX_SERVICE_NAME": f"{stage}-consumer-itest",
        "PYTHONPATH": str(root.parent),
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.consumer_once", "--block-ms", "3_000"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
        check=False,
    )
    assert (
        result.returncode == 0
    ), f"{stage} consumer failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"


def _xrange_json(client: redis_sync.Redis, stream: str) -> list[dict[str, str]]:
    entries = client.xrange(stream)
    return [fields for _id, fields in entries]


# ---------------------------------------------------------------------------
# The bus flow
# ---------------------------------------------------------------------------


class TestSignalToOrderBus:
    def test_full_flow(self, infra: dict[str, str]) -> None:
        client = redis_sync.from_url(infra["redis_url"], decode_responses=True)
        try:
            client.delete(
                "feature.computed",
                "signal.generated",
                "order.request",
                "signal.rejected",
            )

            correlation_id = "itest-correlation-0001"
            client.xadd(
                "feature.computed",
                {
                    "symbol": SYMBOL,
                    "timeframe": "M5",
                    "ts": BAR_TS.isoformat(),
                    "close": "100.0",
                    "rsi": "55.0",
                    "atr_norm": "0.01",
                    "correlation_id": correlation_id,
                },
            )

            # ---- Stage 1: signal-service -------------------------------
            _run_consumer("signal", infra)
            generated = _xrange_json(client, "signal.generated")
            assert len(generated) == 1, f"expected 1 signal, got {len(generated)}"
            assert generated[0]["correlation_id"] == correlation_id
            signal_payload = json.loads(generated[0]["signal"])
            assert signal_payload["direction"] == "BUY"
            assert signal_payload["score"] >= 5
            assert signal_payload["sl"] < signal_payload["entry"] < signal_payload["tp1"]

            # ---- Stage 2: risk-service ---------------------------------
            _run_consumer("risk", infra)
            orders = _xrange_json(client, "order.request")
            rejections = _xrange_json(client, "signal.rejected")
            assert len(orders) == 1, f"expected approved order; rejections={rejections}"
            order = orders[0]
            assert order["direction"] == "BUY"
            assert float(order["lots"]) >= 0.01  # clamp-DOWN respected
            assert order["idempotency_key"] == signal_payload["id"]
            assert order["correlation_id"] == correlation_id
            assert rejections == []
        finally:
            client.close()

    def test_rejection_path_publishes_reason(self, infra: dict[str, str]) -> None:
        """A signal whose SL violates stops level must reach rejected stream."""
        client = redis_sync.from_url(infra["redis_url"], decode_responses=True)
        try:
            # Tighten broker stops so the seeded signal geometry violates it.
            spec = json.loads(client.get(f"mt5:symbol:{SYMBOL}") or "{}")
            spec["stops_level_points"] = 500  # 5.0 price units minimum distance
            client.set(f"mt5:symbol:{SYMBOL}", json.dumps(spec))

            client.xadd(
                "feature.computed",
                {
                    "symbol": SYMBOL,
                    "timeframe": "M15",
                    "ts": (BAR_TS + timedelta(minutes=15)).isoformat(),
                    "close": "100.2",
                    "rsi": "56.0",
                    "correlation_id": "itest-reject-0002",
                },
            )
            _run_consumer("signal", infra)

            # Risk stage now rejects on stops-level violation.
            _run_consumer("risk", infra)
            rejections = _xrange_json(client, "signal.rejected")
            assert len(rejections) >= 1
            reasons = {r.get("rejected_reason") for r in rejections}
            assert "stops_level_violation" in reasons
        finally:
            client.close()
