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
                progress=lambda _n: None,
            )
            logger.info("backfilled", timeframe=timeframe, bars=stored)
    finally:
        client.shutdown()
        await db.close()
    return rc


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

    return asyncio.run(_run_backfill(symbol, args.days, args.timeframes, args.chunk_size))


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
