"""Indicator correctness tests against hand-computed references."""

import numpy as np
import pytest
from vix_core.indicators import atr, bollinger_bands, ema, log_returns, rsi, true_range


def test_ema_constant_series_is_constant() -> None:
    values = np.full(50, 5.0)
    result = ema(values, 10)
    assert np.allclose(result, 5.0)


def test_ema_shape_and_first_value() -> None:
    rng = np.random.default_rng(42)
    values = rng.normal(size=100).cumsum() + 100.0
    result = ema(values, 14)
    assert result.shape == values.shape
    assert result[0] == pytest.approx(values[0])


def test_rsi_bounds_and_warmup_nan() -> None:
    rng = np.random.default_rng(7)
    close = 100.0 + rng.normal(scale=0.5, size=200).cumsum()
    out = rsi(close, period=14)
    assert out.shape == close.shape
    assert np.all(np.isnan(out[:14]))
    valid = out[14:]
    assert np.nanmin(valid) >= 0.0
    assert np.nanmax(valid) <= 100.0


def test_rsi_all_gains_is_100() -> None:
    close = np.linspace(1.0, 50.0, 40)
    out = rsi(close, period=14)
    assert np.nanmax(out[14:]) == pytest.approx(100.0)


def test_atr_matches_manual_first_steps() -> None:
    high = np.array([10.0, 11.0, 12.0, 11.5])
    low = np.array([9.0, 10.0, 10.5, 10.0])
    close = np.array([9.5, 10.5, 11.0, 10.8])
    smoothed = atr(high, low, close, period=3)
    assert smoothed.shape == high.shape
    assert np.isnan(smoothed[0])


def test_bollinger_bands_symmetric() -> None:
    close = np.full(30, 100.0)
    mid, upper, lower = bollinger_bands(close, period=20, num_std=2.0)
    assert np.all(np.isnan(mid[:19]))
    assert np.allclose(mid[19:], 100.0)  # zero std
    assert np.allclose(upper[19:], 100.0)
    assert np.allclose(lower[19:], 100.0)


def test_atr_returns_smoothed_series() -> None:
    high = np.array([10.0, 11.0, 12.0, 11.5])
    low = np.array([9.0, 10.0, 10.5, 10.0])
    close = np.array([9.5, 10.5, 11.0, 10.8])
    smoothed = atr(high, low, close, period=3)
    raw = true_range(high, low, close)
    assert raw[1] == pytest.approx(1.5)
    # Wilder smooth of period 3 == EMA(5): alpha=1/3
    expected_1 = 1.5 / 3.0 + 1.0 * (2.0 / 3.0)
    assert np.isnan(smoothed[0])
    assert smoothed[1] == pytest.approx(expected_1)


def test_log_returns_known_values() -> None:
    close = np.array([100.0, 110.0, 99.0])
    rets = log_returns(close)
    expected = [0.0, float(np.log(1.1)), float(np.log(0.9))]
    assert np.allclose(rets, expected)


def test_short_inputs_do_not_crash() -> None:
    close = np.array([1.0, 2.0])
    assert rsi(close, 14).shape == (2,)
    mid, upper, lower = bollinger_bands(close, 20)
    assert all(a.size == 2 for a in (mid, upper, lower))
