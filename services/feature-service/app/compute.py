"""Feature computation pipeline - strictly vectorized, DB-free.

Pure functions only: bars in, :class:`FeatureFrame` out. Database and Redis
concerns live in ``db.py``/``consumer.py`` so this module is unit-testable
from a static parquet file with no services running.

Warmup contract: every series is fully defined (no NaN) from bar index 199
onward given the standard 500-bar fetch window (EMA200 is the binding
constraint; RSI/ATR/BB/Stoch warm up well before that).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import numpy.typing as npt
from vix_core.indicators import (
    atr,
    bollinger_bands,
    ema,
    log_returns,
    realized_volatility,
    rsi,
    stochastic,
)
from vix_core.logging import get_logger
from vix_core.schemas import Bar, ZoneState
from vix_core.swings import Swing, detect_swings
from vix_core.zones import ZoneEngine

logger = get_logger(__name__)

FloatArray = npt.NDArray[np.float64]
TsArray = npt.NDArray[np.datetime64]

FEATURE_WINDOW = 500  # bars fetched per event (matches spec)
WARMUP_INDEX = 199  # first index guaranteed complete for every series

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _to_ns_array(bars: Sequence[Bar]) -> TsArray:
    """Exact epoch-nanosecond array (avoids tz-naive datetime64 warnings)."""
    micros = np.fromiter(((bar.ts - _EPOCH) // _MICROSECOND for bar in bars), dtype=np.int64)
    return (micros * 1_000).view("datetime64[ns]")


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    """Full-length vectorized feature arrays over one OHLCV window."""

    ts: TsArray
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    atr: FloatArray
    atr_norm: FloatArray
    rsi: FloatArray
    ema50: FloatArray
    ema200: FloatArray
    bb_upper: FloatArray
    bb_mid: FloatArray
    bb_lower: FloatArray
    stoch_k: FloatArray
    stoch_d: FloatArray
    log_return: FloatArray
    realized_vol: FloatArray

    @property
    def length(self) -> int:
        return int(self.close.size)


def build_frame(bars: Sequence[Bar]) -> FeatureFrame:
    """Compute all indicator series over a closed-bar window."""
    if not bars:
        raise ValueError("cannot build frame from empty bars")
    open_ = np.fromiter((b.open for b in bars), dtype=np.float64)
    high = np.fromiter((b.high for b in bars), dtype=np.float64)
    low = np.fromiter((b.low for b in bars), dtype=np.float64)
    close = np.fromiter((b.close for b in bars), dtype=np.float64)
    ts = np.array([np.datetime64(b.ts, "ns") for b in bars], dtype="datetime64[ns]")

    atr_arr = atr(high, low, close, period=14)
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_norm = np.where(close > 0.0, atr_arr / close, np.nan)
    rets = log_returns(close)

    return FeatureFrame(
        ts=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        atr=atr_arr,
        atr_norm=atr_norm,
        rsi=rsi(close, period=14),
        ema50=ema(close, period=50),
        ema200=ema(close, period=200),
        bb_upper=bollinger_bands(close, period=20, num_std=2.0)[1],
        bb_mid=bollinger_bands(close, period=20, num_std=2.0)[0],
        bb_lower=bollinger_bands(close, period=20, num_std=2.0)[2],
        stoch_k=stochastic(high, low, close, k_period=14, d_period=3)[0],
        stoch_d=stochastic(high, low, close, k_period=14, d_period=3)[1],
        log_return=rets,
        realized_vol=realized_volatility(rets, window=21),
    )


def frame_is_complete(frame: FeatureFrame, *, from_index: int = WARMUP_INDEX) -> bool:
    """True when every series is NaN-free from ``from_index`` onward."""
    if frame.length <= from_index:
        return False
    series = (
        frame.atr,
        frame.atr_norm,
        frame.rsi,
        frame.ema50,
        frame.ema200,
        frame.bb_upper,
        frame.bb_mid,
        frame.bb_lower,
        frame.stoch_k,
        frame.stoch_d,
        frame.log_return,
        frame.realized_vol,
    )
    return all(bool(np.all(np.isfinite(s[from_index:]))) for s in series)


def latest_pivots(
    high: FloatArray, low: FloatArray, lookback: int = 5
) -> tuple[float | None, float | None]:
    """(last confirmed swing high price, last confirmed swing low price)."""
    swings: tuple[Swing, ...] = detect_swings(high, low, lookback=lookback)
    highs = [s.price for s in swings if s.kind == "high"]
    lows = [s.price for s in swings if s.kind == "low"]
    return (highs[-1] if highs else None, lows[-1] if lows else None)


def snapshot_from_bars(bars: Sequence[Bar], symbol: str, timeframe: str) -> dict[str, object]:
    """Full feature-service payload for one closed bar (DB row shape)."""
    if len(bars) < WARMUP_INDEX + 1:
        raise ValueError(f"need at least {WARMUP_INDEX + 1} bars, got {len(bars)}")

    frame = build_frame(bars)
    if not frame_is_complete(frame):
        raise ValueError("feature window incomplete; refusing to publish NaNs")

    swing_high, swing_low = latest_pivots(frame.high, frame.low)
    engine = ZoneEngine()
    zones: list[dict[str, object]] = [
        zone.model_dump(mode="json")
        for zone in engine.build_zones(frame.open, frame.high, frame.low, frame.close, frame.ts)
        if zone.state is not ZoneState.BROKEN
    ]

    def last(arr: npt.NDArray[np.float64]) -> float:
        return float(arr[-1])

    logger.debug(
        "frame computed",
        symbol=symbol,
        timeframe=timeframe,
        bars=len(bars),
        zones=len(zones),
    )
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "ts": bars[-1].ts.isoformat(),
        "close": float(frame.close[-1]),
        "atr": last(frame.atr),
        "atr_norm": last(frame.atr_norm),
        "rsi": last(frame.rsi),
        "ema50": last(frame.ema50),
        "ema200": last(frame.ema200),
        "bb_upper": last(frame.bb_upper),
        "bb_mid": last(frame.bb_mid),
        "bb_lower": last(frame.bb_lower),
        "stoch_k": last(frame.stoch_k),
        "stoch_d": last(frame.stoch_d),
        "log_return": last(frame.log_return),
        "realized_vol": last(frame.realized_vol),
        "swing_high": swing_high,
        "swing_low": swing_low,
        "zones": zones,
    }
