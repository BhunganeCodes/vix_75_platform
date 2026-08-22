"""api-gateway: JWT-protected reverse proxy in front of services.

Sprint 1 scope: /health + HS256 JWT verification dependency + a proxy
stub. Real routing table lands with the service mesh wiring in Sprint 2.
Tokens are short-lived and issued out-of-band (ops CLI) - the gateway
never authenticates with broker credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast

import jwt as pyjwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from vix_core.config import Settings
from vix_core.logging import configure_logging, get_logger

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings(service_name="api-gateway")
    if settings.jwt_secret.get_secret_value() == "dev-only-change-me":
        logger.warning("default JWT secret in use; DO NOT run live like this")
    configure_logging(
        settings.service_name, level=settings.log_level, json_output=settings.log_json
    )
    app.state.settings = settings
    yield


app = FastAPI(title="vix75 api-gateway", version="0.1.0", lifespan=lifespan)


async def require_jwt(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        payload = pyjwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    return cast(dict[str, object], payload)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": "api-gateway", "status": "ok"}


@app.get("/secure/ping")
async def secure_ping(
    claims: Annotated[dict[str, object], Depends(require_jwt)],
) -> dict[str, object]:
    """Reference implementation of a JWT-gated route."""
    subject = str(claims.get("sub", "unknown"))
    return {"pong": True, "subject": subject}
