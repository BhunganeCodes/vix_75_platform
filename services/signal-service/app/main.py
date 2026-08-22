"""signal-service skeleton: consumes features, emits gated signals."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger
from vix_core.scoring import ConfluenceScorer

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="signal-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    app.state.scorer = ConfluenceScorer(min_p_win=settings.min_p_win)
    app.state.settings = settings
    yield


app = FastAPI(title="vix75 signal-service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": "signal-service", "status": "ok"}


@app.post("/evaluate")
async def evaluate() -> dict[str, object]:
    """Confluence evaluation of the latest LTF setup - wired in Sprint 2."""
    raise HTTPException(status_code=501, detail="evaluation pipeline lands in Sprint 2")
