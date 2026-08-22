"""MetaTrader5 terminal client wrapper.

Design notes:

* The ``MetaTrader5`` package is Windows-only and therefore an OPTIONAL
  dependency (``pip install vix-core[mt5]``). Every entry point raises
  :class:`MT5UnavailableError` with a clear message when it is missing so the
  same code runs on Linux CI/Oracle Cloud in stub mode.
* All timestamps leaving this module are timezone-aware UTC datetimes
  (the legacy code mixed naive/UTC timestamps - a known source of
  off-by-hours bugs).
* Transient terminal failures are retried with bounded exponential
  backoff. No bare excepts; every failure is logged via structlog.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from vix_core.config import Settings
from vix_core.logging import get_logger
from vix_core.risk import SymbolConstraints
from vix_core.schemas import Bar

logger = get_logger(__name__)

try:  # pragma: no cover - exercised only on Windows terminals
    import MetaTrader5 as _mt5  # noqa: N813 - upstream package name is CamelCase

    _MT5_IMPORT_OK = True
except ImportError:  # pragma: no cover
    _mt5 = None
    _MT5_IMPORT_OK = False


class MT5UnavailableError(RuntimeError):
    """Raised when the MetaTrader5 package/terminal is not usable."""


def require_mt5() -> ModuleType:
    if not _MT5_IMPORT_OK or _mt5 is None:
        raise MT5UnavailableError(
            "MetaTrader5 package not installed on this platform. "
            "Install vix-core[mt5] on the Windows bridge host."
        )
    return _mt5


TIMEFRAME_MAP: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
}


@dataclass(frozen=True, slots=True)
class SymbolSnapshot:
    """Everything risk.py needs from the terminal, snapshotted once."""

    symbol: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    margin_per_lot: float


def _to_utc_bar(row: Any) -> Bar:
    ts = datetime.fromtimestamp(int(row["time"]), tz=UTC)
    return Bar(
        ts=ts,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        tick_volume=int(row["tick_volume"]),
    )


class MT5Client:
    """Thin, retrying, tz-aware wrapper around the raw MT5 API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connected = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, *, retries: int = 3, initial_delay_s: float = 2.0) -> bool:
        mt5 = require_mt5()
        delay = initial_delay_s
        last_error: str | None = None

        for attempt in range(1, retries + 1):
            kwargs: dict[str, Any] = {}
            if self._settings.mt5_terminal_path:
                kwargs["path"] = self._settings.mt5_terminal_path
            if not mt5.initialize(**kwargs):
                last_error = str(mt5.last_error())
                logger.warning(
                    "mt5 initialize failed",
                    attempt=attempt,
                    error=last_error,
                )
                time.sleep(delay)
                delay *= 2
                continue

            if self._settings.mt5_login and self._settings.mt5_password:
                authorised = mt5.login(
                    login=self._settings.mt5_login,
                    password=self._settings.mt5_password.get_secret_value(),
                    server=self._settings.mt5_server,
                )
                if not authorised:
                    last_error = f"login failed: {mt5.last_error()}"
                    logger.warning("mt5 login failed", attempt=attempt)
                    mt5.shutdown()
                    time.sleep(delay)
                    delay *= 2
                    continue

            self._connected = True
            logger.info("mt5 connected", attempts=attempt)
            return True

        raise MT5UnavailableError(f"could not connect after {retries} attempts: {last_error}")

    def shutdown(self) -> None:
        if self._connected and _mt5 is not None:
            _mt5.shutdown()
            self._connected = False
            logger.info("mt5 disconnected")

    def __enter__(self) -> MT5Client:
        if not self._connected:
            self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Market data / trading context
    # ------------------------------------------------------------------

    def copy_bars(self, symbol: str, timeframe: str, count: int) -> tuple[Bar, ...]:
        """Fetch the most recent `count` CLOSED bars as tz-aware Bars."""
        mt5 = require_mt5()
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"unsupported timeframe {timeframe!r}")

        rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME_MAP[timeframe], 0, count + 1)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"copy_rates failed for {symbol}: {mt5.last_error()}")
        bars = tuple(_to_utc_bar(row) for row in rates[:-1])  # drop forming bar
        return bars

    def symbol_snapshot(self, symbol: str) -> SymbolSnapshot:
        mt5 = require_mt5()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info returned None for {symbol}")
        tick = mt5.symbol_info_tick(symbol)
        tick_value = float(getattr(info, "trade_tick_value", 0.0))
        margin = 0.0
        account = mt5.account_info()
        if tick is not None and account is not None:
            order_cost = mt5.order_calc_margin(0, symbol, 1.0, float(tick.ask))
            margin = float(order_cost) if order_cost else 0.0
        return SymbolSnapshot(
            symbol=symbol,
            digits=int(info.digits),
            point=float(info.point),
            tick_size=float(info.trade_tick_size or info.point),
            tick_value=tick_value,
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            stops_level_points=int(info.trade_stops_level),
            margin_per_lot=margin,
        )

    def constraints(self, symbol: str) -> SymbolConstraints:
        snap = self.symbol_snapshot(symbol)
        return SymbolConstraints(
            volume_min=snap.volume_min,
            volume_max=snap.volume_max,
            volume_step=snap.volume_step,
            tick_size=snap.tick_size,
            tick_value=snap.tick_value,
            point=snap.point,
            stops_level_points=snap.stops_level_points,
            margin_per_lot=snap.margin_per_lot,
        )
