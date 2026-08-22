"""execution-service HTTP routes: health, positions, manual close."""

from __future__ import annotations

import asyncio
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from vix_core.logging import get_logger

from .db import ExecutionDatabase
from .mt5_executor import MT5Executor

logger = get_logger(__name__)
router = APIRouter()


def get_db(request: Request) -> ExecutionDatabase:
    return cast(ExecutionDatabase, request.app.state.db)


def get_executor(request: Request) -> MT5Executor:
    return cast(MT5Executor, request.app.state.executor)


@router.get("/health")
async def health(
    db: Annotated[ExecutionDatabase, Depends(get_db)],
    executor: Annotated[MT5Executor, Depends(get_executor)],
) -> dict[str, object]:
    db_ok = await db.ping()
    mt5_state = "available" if executor.mt5 is not None else "unavailable"
    return {
        "service": "execution-service",
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "mt5": mt5_state,
        "mode": ("dry_run" if executor.settings.dry_run_mode else "live"),
    }


@router.get("/positions")
async def positions(
    db: Annotated[ExecutionDatabase, Depends(get_db)],
) -> dict[str, object]:
    """Locally-open filled trades (no secrets, no balances)."""
    rows = await db.open_trades()
    return {"count": len(rows), "positions": rows}


@router.post("/close/{ticket}")
async def close_position(
    ticket: int,
    db: Annotated[ExecutionDatabase, Depends(get_db)],
    executor: Annotated[MT5Executor, Depends(get_executor)],
) -> dict[str, object]:
    trade = await db.trade_by_ticket(ticket)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"open trade with ticket {ticket} not found")

    outcome = await asyncio.to_thread(
        executor.close_position, str(trade["symbol"]), ticket, float(trade["lots"])
    )
    if not outcome.result.accepted:
        outcome_view: dict[str, object] = {
            "accepted": bool(outcome.result.accepted),
            "retcode": outcome.result.retcode,
            "retcode_description": outcome.result.retcode_description,
            "price": outcome.result.price,
        }
        await db.audit(
            "execution-service",
            "position.close",
            str(ticket),
            "error",
            outcome_view,
            error=outcome.last_error or outcome.result.retcode_description,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "retcode": outcome.result.retcode,
                "description": outcome.result.retcode_description,
            },
        )

    profit = 0.0  # realised PnL arrives via reconciliation sweep from deal history
    await db.close_by_ticket(
        ticket,
        profit=profit,
        exit_price=outcome.result.price,
        extra={"source": "manual_close"},
    )
    logger.info("position closed manually", ticket=ticket)
    return {
        "closed": True,
        "ticket": ticket,
        "exit_price": outcome.result.price,
        "note": "realised pnl will be attached by the reconciliation sweep",
    }
