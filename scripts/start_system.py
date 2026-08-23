#!/usr/bin/env python3
"""Master orchestration CLI for the VIX75 platform.

Subcommands
-----------
backfill  Fetch historical OHLCV into TimescaleDB.
          Runs on the WINDOWS BRIDGE host (MetaTrader5 is Windows-only),
          or remotely via the gateway with --via-gateway.
train     Trigger ML training through the ml-service API.
up        docker compose up -d, then wait for TimescaleDB + Redis.
down      Stop the stack.            status   Show compose service state.

Examples::

    python scripts/start_system.py backfill --symbol "Volatility 75 Index" --days 365
    python scripts/start_system.py train --model hmm
    python scripts/start_system.py train --model meta_label --timeframe M15
    python scripts/start_system.py up
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import httpx
from vix_core.config import Settings, get_settings
from vix_core.correlation import CORRELATION_HEADER, new_correlation_id
from vix_core.logging import configure_logging, get_logger

logger = get_logger(__name__)

if sys.platform == "win32":  # pragma: no cover - psycopg3 async requirement
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = REPO_ROOT / "services" / "data-service"

VALID_MODELS = frozenset({"hmm", "meta_label", "both"})
DEFAULT_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _mint_token(settings: Settings) -> str:
    """Mint an ops JWT locally - the CLI shares the gateway secret."""
    from datetime import UTC, datetime, timedelta

    import jwt as pyjwt

    now = datetime.now(tz=UTC)
    claims = {
        "sub": "ops-cli",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    return str(pyjwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256"))


def _auth_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_mint_token(settings)}",
        CORRELATION_HEADER: new_correlation_id(),
    }


def wait_for_tcp(host: str, port: int, name: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> bool:
    """Block until a TCP endpoint accepts connections (or timeout)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                logger.info("dependency ready", name=name, host=host, port=port)
                return True
        except OSError:
            time.sleep(2)
    logger.error("dependency NOT ready", name=name, host=host, port=port)
    return False


def _parse_host_port(url: str, default_port: int) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.hostname or "localhost", parsed.port or default_port


def _compose(args: argparse.Namespace, *cmd: str) -> int:
    compose_files: list[str] = ["-f", str(REPO_ROOT / "docker-compose.yml")]
    if args.prod:
        compose_files += ["-f", str(REPO_ROOT / "docker-compose.prod.yml")]
    full = ["docker", "compose", *compose_files, *cmd]
    logger.info("running", cmd=" ".join(full))
    return subprocess.call(full, cwd=str(REPO_ROOT))  # noqa: S603 - fixed argv


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


async def _run_backfill(symbol: str, days: int, timeframes: list[str], chunk: int) -> int:
    """Direct-MT5 path: reuse the data-service pipeline on the bridge host."""
    sys.path.insert(0, str(SERVICES_DIR))
    from app.backfill import backfill_timeframe  # type: ignore[import-not-found]
    from app.db import Database  # type: ignore[import-not-found]
    from app.mt5_client import BridgeMT5Client  # type: ignore[import-not-found]

    settings = get_settings()
    configure_logging("start-system-backfill", level=settings.log_level)
    client = BridgeMT5Client(settings)

    try:
        await asyncio.to_thread(client.connect)
    except Exception:
        logger.exception("mt5 connection failed")
        return 2

    db = Database(settings.database_url)
    await db.connect()

    rc = 0
    try:
        for timeframe in timeframes:
            stored = await backfill_timeframe(
                client=client,
                db=db,
                symbol=symbol,
                timeframe=timeframe,
                lookback_days=days,
                chunk_size=chunk,
                progress=lambda _tf, _n: None,
            )
            logger.info("backfilled", timeframe=timeframe, bars=stored)
    finally:
        client.shutdown()
        await db.close()
    return rc


def _cmd_features(args: argparse.Namespace) -> int:
    """Compute per-bar feature snapshots over stored OHLCV history.

    Linear-time: all indicator series are built ONCE via vix_core's
    vectorized frame builder (identical math to the live feature-service);
    zones/swings are derived from the full window and attached per row.
    """
    sys.path.insert(0, str(REPO_ROOT / "services" / "feature-service"))
    import numpy as np
    import numpy.typing as npt
    import psycopg
    from app.compute import (  # type: ignore[import-not-found]
        WARMUP_INDEX,
        build_frame,
        latest_pivots,
    )
    from psycopg.types.json import Jsonb
    from vix_core.schemas import Bar
    from vix_core.zones import ZoneEngine

    settings = get_settings()
    configure_logging("start-system-features", level=settings.log_level)
    symbol = args.symbol or settings.symbol

    bars: list[Bar] = []
    dsn = settings.database_url
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, tick_volume FROM ohlcv"
            " WHERE symbol=%s AND timeframe=%s ORDER BY ts ASC",
            (symbol, args.timeframe),
        )
        for r in cur.fetchall():
            bars.append(
                Bar(
                    ts=r[0],
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    tick_volume=int(r[5] or 0),
                )
            )
    logger.info("history loaded", timeframe=args.timeframe, bars=len(bars))
    if len(bars) <= WARMUP_INDEX + 1:
        logger.error("insufficient history", need=WARMUP_INDEX + 2, have=len(bars))
        return 1

    frame = build_frame(bars)

    # Zones computed once over the full window (same detector the live
    # consumer runs); broken zones excluded from every row snapshot.
    engine = ZoneEngine()
    active_zones = [
        z.model_dump(mode="json")
        for z in engine.build_zones(frame.open, frame.high, frame.low, frame.close, frame.ts)
        if z.state.value != "broken"
    ]
    swing_high, swing_low = latest_pivots(frame.high, frame.low)

    def f(arr: npt.NDArray[np.float64], idx: int) -> float | None:
        v = float(arr[idx])
        return v if np.isfinite(v) else None

    insert_sql = (
        "INSERT INTO features (symbol,timeframe,ts,close,atr,atr_norm,rsi,"
        "ema50,ema200,bb_upper,bb_mid,bb_lower,stoch_k,stoch_d,realized_vol,"
        "log_return,swing_high,swing_low,zones) VALUES (%s,%s,%s,%s,%s,%s,%s,"
        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (symbol,timeframe,ts) DO UPDATE SET close=EXCLUDED.close,"
        " atr=EXCLUDED.atr, rsi=EXCLUDED.rsi, ema50=EXCLUDED.ema50,"
        " ema200=EXCLUDED.ema200"
    )

    payload: list[tuple[object, ...]] = []
    append = payload.append
    ts_arr = frame.ts
    for i in range(WARMUP_INDEX + 1, len(bars)):
        append(
            (
                symbol,
                args.timeframe,
                bars[i].ts,
                f(frame.close, i),
                f(frame.atr, i),
                f(frame.atr_norm, i),
                f(frame.rsi, i),
                f(frame.ema50, i),
                f(frame.ema200, i),
                f(frame.bb_upper, i),
                f(frame.bb_mid, i),
                f(frame.bb_lower, i),
                f(frame.stoch_k, i),
                f(frame.stoch_d, i),
                f(frame.realized_vol, i),
                f(frame.log_return, i),
                swing_high,
                swing_low,
                Jsonb(active_zones),
            )
        )

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.executemany(insert_sql, payload)

    latest_ts = np.datetime_as_string(ts_arr[-1], unit="s")
    logger.info(
        "feature backfill complete",
        timeframe=args.timeframe,
        rows_written=len(payload),
        latest=str(latest_ts),
        zones=len(active_zones),
    )
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    symbol = args.symbol or get_settings().symbol

    if args.via_gateway:
        url = f"{args.gateway_url}/data/backfill"
        response = httpx.post(
            url,
            json={"timeframes": args.timeframes, "lookback_days": args.days},
            headers=_auth_headers(get_settings()),
            timeout=30,
        )
        logger.info("gateway backfill queued", status=response.status_code)
        return 0 if response.status_code == 202 else 1

    try:
        return asyncio.run(_run_backfill(symbol, args.days, args.timeframes, args.chunk_size))
    except KeyboardInterrupt:
        logger.warning("backfill interrupted")
        return 130
    except Exception:
        logger.exception("backfill failed")
        return 2


# ---------------------------------------------------------------------------
# train
# ---------------------------------------------------------------------------


def _cmd_train(args: argparse.Namespace) -> int:
    models = sorted(VALID_MODELS) if args.model == "both" else [args.model]

    results: list[int] = []
    for model in models:
        url = f"{args.gateway_url}/ml/train?timeframe={args.timeframe}"
        response = httpx.post(
            url,
            headers=_auth_headers(get_settings()),
            timeout=30,
        )
        ok = response.status_code == 202
        results.append(0 if ok else 1)
        logger.info(
            "training request sent",
            model=model,
            timeframe=args.timeframe,
            status=response.status_code,
        )
    return max(results) if results else 0


# ---------------------------------------------------------------------------
# up / down / status
# ---------------------------------------------------------------------------


def _cmd_up(args: argparse.Namespace) -> int:
    rc = _compose(args, "up", "-d", "--build")
    if rc != 0:
        return rc

    settings = get_settings()
    pg_host, pg_port = _parse_host_port(settings.database_url, 5432)
    redis_host, redis_port = _parse_host_port(settings.redis_url, 6379)

    ok = True
    ok &= wait_for_tcp(pg_host, pg_port, "timescaledb")
    ok &= wait_for_tcp(redis_host, redis_port, "redis")
    # Gateway only publishes in dev; in prod it sits behind Caddy.
    gw_host, gw_port = _parse_host_port(args.gateway_url, 80)
    ok &= wait_for_tcp(gw_host, gw_port, "api-gateway")

    if not ok:
        logger.error("stack unhealthy; check `docker compose logs`")
        return 1
    logger.info("stack is up and healthy")
    return 0


def _cmd_down(args: argparse.Namespace) -> int:
    return _compose(args, "down")


def _cmd_status(args: argparse.Namespace) -> int:
    return _compose(args, "ps")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--gateway-url", default="http://localhost:8000")
        p.add_argument("--prod", action="store_true", help="use docker-compose.prod.yml overlay")

    bf = sub.add_parser("backfill", help="fetch historical OHLCV into TimescaleDB")
    bf.add_argument("--symbol", default=None)
    bf.add_argument("--days", type=int, default=365)
    bf.add_argument("--timeframes", nargs="+", default=["M5", "M15", "H1", "H4"])
    bf.add_argument("--chunk-size", type=int, default=10_000)
    bf.add_argument(
        "--via-gateway",
        action="store_true",
        help="POST to data-service instead of local MT5",
    )
    add_common(bf)
    bf.set_defaults(func=_cmd_backfill)

    ft = sub.add_parser("features", help="backfill per-bar feature snapshots from stored OHLCV")
    ft.add_argument("--symbol", default=None)
    ft.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1", "H4"])
    add_common(ft)
    ft.set_defaults(func=_cmd_features)

    tr = sub.add_parser("train", help="trigger ML training via the ml-service")
    tr.add_argument("--model", choices=sorted(VALID_MODELS), required=True)
    tr.add_argument("--timeframe", default="M15", choices=["M5", "M15", "H1"])
    add_common(tr)
    tr.set_defaults(func=_cmd_train)

    up = sub.add_parser("up", help="start the stack and wait for dependencies")
    add_common(up)
    up.set_defaults(func=_cmd_up)

    dn = sub.add_parser("down", help="stop the stack")
    add_common(dn)
    dn.set_defaults(func=_cmd_down)

    st = sub.add_parser("status", help="show compose service state")
    add_common(st)
    st.set_defaults(func=_cmd_status)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    configure_logging("start-system", level=settings.log_level)
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return int(result)


if __name__ == "__main__":
    sys.exit(main())
