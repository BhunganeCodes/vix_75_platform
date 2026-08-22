"""data-service HTTP routes: health + backfill trigger."""

from __future__ import annotations

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from vix_core.config import Settings
from vix_core.correlation import CORRELATION_HEADER
from vix_core.logging import get_logger

from .backfill import backfill_timeframe
from .db import Database
from .ingest import OHLCV_STREAM, Ingestor
from .mt5_client import INGEST_TIMEFRAMES

logger = get_logger(__name__)
router = APIRouter()


def get_db(request: Request) -> Database:
    return cast(Database, request.app.state.db)


def get_ingestor(request: Request) -> Ingestor:
    return cast(Ingestor, request.app.state.ingestor)


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


@router.get("/health")
async def health(
    db: Annotated[Database, Depends(get_db)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
) -> dict[str, object]:
    """Liveness + dependency truthfulness. Never exposes balances/logins."""
    db_ok = await db.ping()
    if ingestor.client._connected:
        mt5_state = "connected"
    elif not ingestor.mt5_available:
        mt5_state = "unavailable"
    else:
        mt5_state = "standby"
    stats = ingestor.stats
    return {
        "service": "data-service",
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "mt5": mt5_state,
        "cycles": stats.cycles,
        "bars_written": stats.bars_written,
        "events_published": stats.events_published,
        "errors": stats.errors,
        "last_bar_ts": stats.last_bar_ts,
    }


class BackfillRequest(BaseModel):
    timeframes: tuple[str, ...] = INGEST_TIMEFRAMES
    lookback_days: int = Field(default=1826, ge=1, le=7300)  # ~5y default
    chunk_size: int = Field(default=10_000, ge=100, le=100_000)


@router.post("/backfill", status_code=202)
async def trigger_backfill(
    req: BackfillRequest,
    background: BackgroundTasks,
    db: Annotated[Database, Depends(get_db)],
    ingestor: Annotated[Ingestor, Depends(get_ingestor)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    x_correlation_id: Annotated[str | None, Query(alias=CORRELATION_HEADER)] = None,
) -> dict[str, object]:
    """Queue a chunked historical fetch; returns 202 immediately."""
    unknown = [tf for tf in req.timeframes if tf not in INGEST_TIMEFRAMES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unsupported timeframes {unknown}")
    if not ingestor.mt5_available:
        raise HTTPException(status_code=503, detail="MT5 bridge unavailable on this host")

    correlation_id = x_correlation_id or uuid.uuid4().hex

    async def _job() -> None:
        for timeframe in req.timeframes:
            await backfill_timeframe(
                client=ingestor.client,
                db=db,
                symbol=settings.symbol,
                timeframe=timeframe,
                lookback_days=req.lookback_days,
                chunk_size=req.chunk_size,
            )

    background.add_task(_job)
    logger.info(
        "backfill queued",
        correlation_id=correlation_id,
        timeframes=list(req.timeframes),
    )
    return {
        "queued": True,
        "timeframes": list(req.timeframes),
        "lookback_days": req.lookback_days,
        "stream": OHLCV_STREAM,
    }
