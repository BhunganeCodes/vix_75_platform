"""notify-service skeleton: Telegram alerts + lifecycle logging.

The Telegram token is a SecretStr from Settings and is never logged
(redaction also guards against accidental key=value leaks).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

logger = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class NotifyRequest(BaseModel):
    event: str  # e.g. "signal.proposed", "order.filled"
    message: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="notify-service")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(base_url=TELEGRAM_API, timeout=10.0)
    yield
    await app.state.http.aclose()


app = FastAPI(title="vix75 notify-service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    settings: Settings = app.state.settings
    return {
        "service": "notify-service",
        "status": "ok",
        "telegram_configured": bool(settings.telegram_token.get_secret_value().startswith("7")),
    }


@app.post("/notify")
async def notify(req: NotifyRequest) -> dict[str, object]:
    settings: Settings = app.state.settings
    token = settings.telegram_token.get_secret_value()
    chat_id = settings.telegram_chat_id
    if "..." in token or not chat_id.isdigit():
        logger.info(
            "telegram not configured; lifecycle log only",
            notification_event=req.event,
        )
        return {"sent": False, "reason": "telegram_unconfigured"}

    try:
        response = await app.state.http.post(
            f"/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"[VIX75] {req.event}\n{req.message}"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("telegram send failed", notification_event=req.event)
        raise HTTPException(status_code=502, detail="telegram delivery failed") from exc

    logger.info("notification sent", notification_event=req.event)
    return {"sent": True}
