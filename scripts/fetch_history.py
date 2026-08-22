#!/usr/bin/env python3
"""Backfill historical VIX75 OHLCV from MT5 into TimescaleDB.

Bootstraps the database with years of history (default: 5y M1) in bounded
chunks so memory stays flat regardless of depth. Reuses the data-service
backfill pipeline (chunked fetch + idempotent batch upserts) - no logic is
duplicated here.

Runs on the LOCAL WINDOWS BRIDGE (MetaTrader5 is Windows-only).

Examples::

    python scripts/fetch_history.py --timeframes M1 --lookback-days 1826
    python scripts/fetch_history.py --timeframes M15 H1 --lookback-days 365 --dry-run

Secrets come from .env via vix_core.config; nothing is ever printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

# Allow reuse of the data-service modules from this standalone script without
# duplicating the backfill pipeline (uv workspace members are not installable).
_SERVICES_DIR = Path(__file__).resolve().parents[1] / "services" / "data-service"
sys.path.insert(0, str(_SERVICES_DIR))

from app.backfill import backfill_timeframe  # type: ignore[import-not-found] # noqa: E402
from app.db import Database  # type: ignore[import-not-found] # noqa: E402
from app.mt5_client import (  # type: ignore[import-not-found] # noqa: E402
    INGEST_TIMEFRAMES,
    BridgeMT5Client,
)
from tqdm import tqdm  # noqa: E402
from vix_core.config import Settings, get_settings  # noqa: E402
from vix_core.logging import configure_logging, get_logger  # noqa: E402

logger = get_logger(__name__)

VALID_TIMEFRAMES: frozenset[str] = frozenset(INGEST_TIMEFRAMES) | {"M30", "H4", "D1"}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeframes",
        nargs="+",
        default=["M1"],
        choices=sorted(VALID_TIMEFRAMES),
        help="timeframes to backfill (default: M1)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=1826,
        help="history depth in days (default: 1826 = ~5 years)",
    )
    parser.add_argument("--symbol", default=None, help="override settings symbol")
    parser.add_argument("--chunk-size", type=int, default=10_000)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--dry-run", action="store_true", help="fetch and log counts only")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings: Settings = get_settings()
    configure_logging("fetch-history", level=settings.log_level)

    symbol = args.symbol or settings.symbol
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
        for timeframe in args.timeframes:
            pbar = tqdm(unit="bars", desc=f"{symbol} {timeframe}", dynamic_ncols=True)

            def _progress(_name: str, n: int, *, bar: tqdm = pbar) -> None:
                bar.update(n)

            if args.dry_run:
                fetched = sum(
                    len(chunk)
                    for chunk in client.iter_history_chunks(
                        symbol,
                        timeframe,
                        lookback_days=args.lookback_days,
                        chunk_size=args.chunk_size,
                    )
                )
                pbar.update(fetched)
            else:
                stored = await backfill_timeframe(
                    client=client,
                    db=db,
                    symbol=symbol,
                    timeframe=timeframe,
                    lookback_days=args.lookback_days,
                    chunk_size=args.chunk_size,
                    progress=_progress,
                )
                logger.info("stored bars", timeframe=timeframe, total=stored)
            pbar.close()
    except Exception:
        logger.exception("backfill failed")
        rc = 1
    finally:
        client.shutdown()
        await db.close()
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
