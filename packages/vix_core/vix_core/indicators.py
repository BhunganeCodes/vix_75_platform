"""Vectorized market indicators implemented on plain numpy arrays.

Constraints honoured here:

* NO pandas_ta, NO TA-Lib - everything is hand-rolled and vectorized.
* All functions are pure: ``array in -> array out``, same length as the
  input, with NaN padding where a lookback window is not yet satisfied.
* Wilder smoothing is expressed as an EMA with ``alpha = 1/period``
  which matches MT5's ATR/RSI definitions closely enough for
  cross-checking against terminal values.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "atr",
    "bollinger_bands",
    "ema",
    "log_returns",
    "realized_volatility",
    "rsi",
    "stochastic",
]


def _validate_ohlc(high: FloatArray, low: FloatArray, close: FloatArray | None = None) -> None:
    if high.ndim != 1 or low.ndim != 1:
        raise ValueError("high/low must be 1-D arrays")
    if close is not None and close.shape != high.shape:
        raise ValueError("close shape must match high/low")


def true_range(high: FloatArray, low: FloatArray, close: FloatArray) -> FloatArray:
    _validate_ohlc(high, low, close)
    if high.size == 0:
        return np.empty(0, dtype=np.float64)
    prev_close = np.concatenate(([close[0]], close[:-1]))
    ranges = np.stack([high - low, np.abs(high - prev_close), np.abs(low - prev_close)], axis=1)
    tr = ranges.max(axis=1)
    tr[0] = high[0] - low[0]
    return tr


def ema(values: FloatArray, period: int) -> FloatArray:
    """Exponential moving average; alpha = 2/(period+1). Seed = first value.

    numpy has no native EMA primitive; this tight O(n) loop over scalars
    is the standard approach without pulling in extra dependencies.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if values.size == 0:
        return np.empty(0, dtype=np.float64)

    alpha = 2.0 / (period + 1.0)
    decay = 1.0 - alpha
    result = np.empty_like(values)
    result[0] = values[0]
    acc = values[0]
    for i in range(1, values.size):
        acc = values[i] * alpha + decay * acc
        result[i] = acc
    return result


def wilder_smooth(values: FloatArray, period: int) -> FloatArray:
    """Wilder smoothing (alpha = 1/period) used by ATR/RSI."""
    return ema(values, 2 * period - 1)


def atr(
    high: FloatArray,
    low: FloatArray,
    close: FloatArray,
    period: int = 14,
) -> FloatArray:
    """Average True Range with Wilder smoothing; NaN for first bar."""
    if high.size == 0:
        return np.empty(0, dtype=np.float64)
    tr = true_range(high, low, close)
    smooth = wilder_smooth(tr, period)
    smooth[0] = np.nan
    return smooth


def rsi(close: FloatArray, period: int = 14) -> FloatArray:
    """Relative Strength Index (Wilder); NaN until `period` bars exist."""
    if close.size <= period:
        return np.full(close.shape, np.nan, dtype=np.float64)
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0.0, delta, 0.0)
    loss = np.where(delta < 0.0, -delta, 0.0)

    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - (100.0 / (1.0 + rs))
    out = np.where(avg_loss == 0.0, 100.0, out)
    out[:period] = np.nan
    return out


def bollinger_bands(
    close: FloatArray, period: int = 20, num_std: float = 2.0
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Returns (middle, upper, lower). NaN outside the rolling window."""
    n = close.size
    mid = np.full(n, np.nan, dtype=np.float64)
    upper = np.full(n, np.nan, dtype=np.float64)
    lower = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return mid, upper, lower

    windows = np.lib.stride_tricks.sliding_window_view(close, period)
    means = windows.mean(axis=1)
    stds = windows.std(axis=1, ddof=0)
    mid[period - 1 :] = means
    upper[period - 1 :] = means + num_std * stds
    lower[period - 1 :] = means - num_std * stds
    return mid, upper, lower


def log_returns(close: FloatArray) -> FloatArray:
    """r_t = ln(C_t / C_{t-1}); element 0 is 0.0."""
    if close.size == 0:
        return np.empty(0, dtype=np.float64)
    shifted = np.concatenate(([close[0]], close[:-1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.log(np.where(shifted > 0.0, close / shifted, 1.0))
    rets[0] = 0.0
    return rets


def realized_volatility(returns: FloatArray, window: int = 20) -> FloatArray:
    """Rolling standard deviation of returns (per-bar vol)."""
    n = returns.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(returns, window)
    out[window - 1 :] = windows.std(axis=1, ddof=1)
    return out


def stochastic(
    high: FloatArray,
    low: FloatArray,
    close: FloatArray,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[FloatArray, FloatArray]:
    """Stochastic Oscillator: returns (%K, %D), both bounded [0, 100].

    %K = 100 * (close - lowest_low(k)) / (highest_high(k) - lowest_low(k));
    NaN until ``k_period`` bars exist. %D is a simple moving average of %K.
    """
    _validate_ohlc(high, low, close)
    n = high.size
    k = np.full(n, np.nan, dtype=np.float64)
    if n < k_period:
        return k, np.full(n, np.nan, dtype=np.float64)

    highest = np.lib.stride_tricks.sliding_window_view(high, k_period).max(axis=1)
    lowest = np.lib.stride_tricks.sliding_window_view(low, k_period).min(axis=1)
    denom = highest - lowest
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.where(denom > 0.0, 100.0 * (close[k_period - 1 :] - lowest) / denom, 50.0)
    k[k_period - 1 :] = raw

    d = np.full(n, np.nan, dtype=np.float64)
    if raw.size >= d_period:
        windows = np.lib.stride_tricks.sliding_window_view(raw, d_period)
        d[k_period + d_period - 2 :] = windows.mean(axis=1)
    return k, d
