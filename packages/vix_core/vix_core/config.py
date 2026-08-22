"""Typed application settings (Pydantic BaseSettings).

Values resolve in this order (highest wins):

1. Process environment variables (container runtime).
2. A ``.env`` file at the current working directory (local dev only).
3. Defaults declared here.

Security contract:

* Credentials are ``SecretStr`` so they never appear in tracebacks or
  ``repr()``.
* Field names deliberately match the legacy env names (``MT5_LOGIN``,
  ``TELEGRAM_TOKEN``, ...) because pydantic-settings matches
  case-insensitively. Infra URLs accept either plain or ``VIX_``
  prefixed names for container friendliness.
* The checked-in ``.env`` holds COMPROMISED credentials used strictly
  for local testing until rotation lands (Docker secrets / Vault).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYMBOL = "Volatility 75 Index"


class Environment(StrEnum):
    LOCAL = "local"
    SHADOW = "shadow"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Identity ----------------------------------------------------
    service_name: str = "unknown-service"
    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    log_json: bool = False

    # ---- MetaTrader 5 bridge (local Windows box ONLY) -----------------
    mt5_login: int = 0
    mt5_password: SecretStr = SecretStr("")
    mt5_server: str = ""
    mt5_terminal_path: str | None = None

    # ---- Broker / notification secrets --------------------------------
    deriv_api_token: SecretStr = SecretStr("")
    telegram_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # ---- Infrastructure ------------------------------------------------
    database_url: str = Field(
        default="postgresql://vix:vix_dev_password@localhost:5432/vix75",
        validation_alias=AliasChoices("DATABASE_URL", "VIX_DATABASE_URL"),
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=AliasChoices("REDIS_URL", "VIX_REDIS_URL"),
    )
    jwt_secret: SecretStr = Field(
        default=SecretStr("dev-only-change-me"),
        validation_alias=AliasChoices("JWT_SECRET", "VIX_JWT_SECRET"),
    )

    # ---- Trading parameters --------------------------------------------
    symbol: str = DEFAULT_SYMBOL
    risk_pct_per_trade: float = Field(default=1.0, ge=0.05, le=2.0)
    min_p_win: float = Field(default=0.55, ge=0.5, lt=1.0)
    shadow_mode: bool = True
    max_open_positions: int = 3
    poll_interval_seconds: float = 5.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor - services must call this instead of Settings()."""
    return Settings()
