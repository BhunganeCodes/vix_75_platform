"""execution-service skeleton: MT5 order placement (local bridge only).

Sprint 1 scope: health + idempotent order-intent contract. The actual
``OrderSend`` path with retcode handling lands in Sprint 2/3 and MUST
only run on the Windows host that hosts the MT5 terminal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger
from vix_core.schemas import OrderRequest, OrderResult

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="execution-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    app.state.settings = settings
    app.state.seen_keys = set()
    if settings.shadow_mode:
        logger.info("execution in SHADOW mode - orders will not reach the broker")
    yield


app = FastAPI(title="vix75 execution-service", version="0.1.0", lifespan=lifespan)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_seen_keys(request: Request) -> set[str]:
    return request.app.state.seen_keys


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": "execution-service", "status": "ok"}


@app.post("/order", response_model=OrderResult)
async def place_order(
    order: OrderRequest,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    seen_keys: Annotated[set[str], Depends(get_seen_keys)],
) -> OrderResult:
    """Idempotent order intent.

    Duplicate ``idempotency_key`` replays a suppression outcome instead
    of double-firing; every code path returns an explicit retcode-style
    result so failures are never silent (audit finding fix).
    """
    if order.idempotency_key in seen_keys:
        logger.info("duplicate order suppressed", key=order.idempotency_key)
        return OrderResult(
            idempotency_key=order.idempotency_key,
            accepted=False,
            retcode_description="DUPLICATE_SUPPRESSED",
            comment="idempotency replay",
        )
    seen_keys.add(order.idempotency_key)

    logger.info(
        "order intent",
        key=order.idempotency_key,
        signal_id=order.signal_id,
        direction=str(order.direction),
        lots=order.lots,
    )
    await asyncio.sleep(0)

    if settings.shadow_mode:
        return OrderResult(
            idempotency_key=order.idempotency_key,
            accepted=False,
            retcode_description="SHADOW_MODE",
            comment="no live execution during shadow phase",
        )
    raise HTTPException(status_code=501, detail="live send path lands in Sprint 3")
