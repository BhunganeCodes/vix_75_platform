"""risk-service: position sizing + exposure guardrails.

Functional from day one - it delegates all math to the pure
``vix_core.risk.compute_lots`` which enforces clamp-DOWN semantics,
stops-level and margin validation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger
from vix_core.risk import SizingStatus, SymbolConstraints, compute_lots

logger = get_logger(__name__)


class SizeRequest(BaseModel):
    symbol: str = "Volatility 75 Index"
    equity: float = Field(gt=0)
    entry: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    constraints: dict[str, float | int]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="risk-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    app.state.settings = settings
    yield


app = FastAPI(title="vix75 risk-service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": "risk-service", "status": "ok"}


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


@app.post("/size")
async def size(
    req: SizeRequest,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> dict[str, object]:
    try:
        constraints = SymbolConstraints(
            volume_min=float(req.constraints["volume_min"]),
            volume_max=float(req.constraints["volume_max"]),
            volume_step=float(req.constraints["volume_step"]),
            tick_size=float(req.constraints["tick_size"]),
            tick_value=float(req.constraints["tick_value"]),
            point=float(req.constraints["point"]),
            stops_level_points=int(req.constraints["stops_level_points"]),
            margin_per_lot=float(req.constraints["margin_per_lot"]),
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing constraint {exc}") from exc

    result = compute_lots(
        equity=req.equity,
        risk_pct=settings.risk_pct_per_trade,
        entry=req.entry,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        constraints=constraints,
    )
    logger.info(
        "sized",
        symbol=req.symbol,
        status=result.status,
        lots=result.lots,
        reason=result.reason,
    )
    return {
        "status": str(result.status),
        "lots": result.lots,
        "reason": result.reason,
        "accepted": result.status in (SizingStatus.OK, SizingStatus.CAPPED_BY_VOLUME_MAX),
    }
