"""JWT authentication for the gateway (PyJWT, HS256).

Credentials currently come from settings (backed by .env locally); in
production they move to Docker secrets / Oracle Vault alongside the rest.

* ``POST /token`` exchanges username+password for a signed JWT.
* ``verify_token`` dependency protects every proxied route and returns
  the decoded claims (``sub`` feeds the rate limiter).
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from vix_core.config import Settings
from vix_core.logging import get_logger

logger = get_logger(__name__)

_ALGORITHMS = ["HS256"]
_bearer_scheme = HTTPBearer(auto_error=False)


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 scheme name
    expires_in: int


def create_token(settings: Settings, subject: str) -> tuple[str, int]:
    """Sign a JWT; returns (token, expires_in_seconds)."""
    expires_in = settings.token_expire_minutes * 60
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = pyjwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    return str(token), expires_in


async def verify_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> dict[str, Any]:
    """FastAPI dependency: 401 unless a valid bearer JWT is presented."""
    settings: Settings = request.app.state.settings
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        payload = pyjwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=_ALGORITHMS,
            options={"require": ["exp", "sub"]},
        )
    except pyjwt.PyJWTError:
        logger.info("jwt rejected")
        raise HTTPException(status_code=401, detail="invalid or expired token") from None
    return payload


def build_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.post("/token", response_model=TokenResponse)
    async def issue_token(body: TokenRequest) -> TokenResponse:
        expected_user = settings.gateway_username
        expected_pass = settings.gateway_password.get_secret_value()
        user_ok = hmac.compare_digest(body.username, expected_user)
        pass_ok = hmac.compare_digest(body.password, expected_pass)
        if not (user_ok and pass_ok):
            logger.warning("token issuance denied", attempted_user=body.username[:32])
            raise HTTPException(status_code=401, detail="invalid credentials")

        token, expires_in = create_token(settings, subject=body.username)
        logger.info("token issued", subject=body.username)
        return TokenResponse(access_token=token, expires_in=expires_in)

    return router
