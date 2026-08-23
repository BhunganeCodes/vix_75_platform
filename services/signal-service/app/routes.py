"""signal-service HTTP routes: health + signal history."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from vix_core.config import Settings

from .consumer import SignalConsumer
from .db import SignalDatabase

router = APIRouter()


def get_db(request: Request) -> SignalDatabase:
    return cast(SignalDatabase, request.app.state.db)


def get_consumer(request: Request) -> SignalConsumer:
    return cast(SignalConsumer, request.app.state.consumer)


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


@router.get("/health")
async def health(
    db: Annotated[SignalDatabase, Depends(get_db)],
    consumer: Annotated[SignalConsumer, Depends(get_consumer)],
) -> dict[str, object]:
    db_ok = await db.ping()
    return {
        "service": "signal-service",
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "events_processed": consumer.processed,
        "signals_fired": consumer.signals_fired,
    }


@router.get("/history")
async def signals_history(
    db: Annotated[SignalDatabase, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    symbol: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """Most recent gated signals from the TimescaleDB signals table."""
    rows = await db.history(symbol, limit)
    return {"count": len(rows), "signals": rows}
