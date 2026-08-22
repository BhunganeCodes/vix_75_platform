"""Feature pipeline tests: static OHLCV parquet -> compute -> assertions.

The parquet fixture is generated deterministically (seeded random walk with
an injected impulse for zone detection) and read back via pyarrow, mirroring
how historical windows reach the service.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyarrow")

import pyarrow as pa
import pyarrow.parquet as pq
from app.compute import (
    FEATURE_WINDOW,
    WARMUP_INDEX,
    build_frame,
    frame_is_complete,
    latest_pivots,
    snapshot_from_bars,
)
from vix_core.schemas import Bar


def _synthetic_bars(n: int = FEATURE_WINDOW) -> list[Bar]:
    rng = np.random.default_rng(42)
    drift = np.cumsum(rng.normal(scale=0.15, size=n))
    close = 100.0 + drift
    open_ = np.concatenate(([100.0], close[:-1]))
    spread = np.abs(rng.normal(scale=0.08, size=n))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread

    # Inject a demand-side impulse leaving a base behind it (mid-series only).
    if n > 320:
        impulse_idx = 300
        close[impulse_idx] = low[impulse_idx - 1] + 3.0
        high[impulse_idx] = close[impulse_idx] + 0.05
        open_[impulse_idx] = low[impulse_idx - 1] + 0.05

    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Bar(
            ts=start + timedelta(minutes=15 * i),
            open=float(open_[i]),
            high=float(high[i]),
            low=float(low[i]),
            close=float(close[i]),
            tick_volume=1_000,
        )
        for i in range(n)
    ]


@pytest.fixture(scope="module")
def parquet_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    bars = _synthetic_bars()
    table = pa.table(
        {
            "ts": pa.array([b.ts for b in bars], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([b.open for b in bars]),
            "high": pa.array([b.high for b in bars]),
            "low": pa.array([b.low for b in bars]),
            "close": pa.array([b.close for b in bars]),
            "tick_volume": pa.array([b.tick_volume for b in bars], type=pa.int64()),
        }
    )
    path = tmp_path_factory.mktemp("data") / "ohlcv_m15.parquet"
    pq.write_table(table, path)
    return path


@pytest.fixture(scope="module")
def bars_from_parquet(parquet_path: Path) -> list[Bar]:
    table = pq.read_table(parquet_path)
    columns = table.to_pydict()
    return [
        Bar(
            ts=row[0],
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            tick_volume=int(row[5]),
        )
        for row in zip(
            columns["ts"],
            columns["open"],
            columns["high"],
            columns["low"],
            columns["close"],
            columns["tick_volume"],
            strict=True,
        )
    ]


class TestFeatureFrame:
    def test_no_nans_from_row_200(self, bars_from_parquet: list[Bar]) -> None:
        frame = build_frame(bars_from_parquet)
        assert frame.length == FEATURE_WINDOW
        assert frame_is_complete(frame)

        series = [
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
        ]
        for arr in series:
            assert np.all(np.isfinite(arr[WARMUP_INDEX:])), "NaN found after warmup"

    def test_warmup_region_has_expected_nans(self, bars_from_parquet: list[Bar]) -> None:
        frame = build_frame(bars_from_parquet)
        assert np.isnan(frame.rsi[:14]).all()
        assert np.isnan(frame.atr[0])
        assert np.isnan(frame.stoch_k[:13]).all()
        assert np.isnan(frame.stoch_d[:15]).all()
        assert np.isnan(frame.realized_vol[:20]).all()

    def test_indicator_bounds(self, bars_from_parquet: list[Bar]) -> None:
        frame = build_frame(bars_from_parquet)
        valid_rsi = frame.rsi[WARMUP_INDEX:]
        assert np.nanmin(valid_rsi) >= 0.0
        assert np.nanmax(valid_rsi) <= 100.0

        k_valid = frame.stoch_k[WARMUP_INDEX:]
        assert np.nanmin(k_valid) >= 0.0
        assert np.nanmax(k_valid) <= 100.0

    def test_mathematical_reference_values(self, bars_from_parquet: list[Bar]) -> None:
        """EMA of a constant series must equal the constant; ATR must be > 0."""
        closes = np.array([b.close for b in bars_from_parquet])
        highs = np.array([b.high for b in bars_from_parquet])
        lows = np.array([b.low for b in bars_from_parquet])

        frame = build_frame(bars_from_parquet)
        # EMA50 tracks the last value closely on smooth data: within 1%.
        assert abs(frame.ema50[-1] - closes[-1]) / closes[-1] < 0.01
        # ATR equals mean true range scale: positive and same order as range.
        tr_last = float(highs[-1] - lows[-1])
        assert 0.0 < frame.atr[-1] < 10 * max(tr_last, 1e-9)
        # Bollinger mid is a rolling mean of close.
        window_mean = float(np.mean(closes[-20:]))
        assert frame.bb_mid[-1] == pytest.approx(window_mean, rel=1e-9)
        # BB symmetry around the midline.
        assert (
            abs((frame.bb_upper[-1] - frame.bb_mid[-1]) - (frame.bb_mid[-1] - frame.bb_lower[-1]))
            < 1e-9
        )

    def test_pivots_detected(self, bars_from_parquet: list[Bar]) -> None:
        highs = np.array([b.high for b in bars_from_parquet])
        lows = np.array([b.low for b in bars_from_parquet])
        swing_high, swing_low = latest_pivots(highs, lows)
        assert swing_high is not None and swing_low is not None
        assert swing_high >= swing_low


class TestSnapshot:
    def test_snapshot_shape_and_values(self, bars_from_parquet: list[Bar]) -> None:
        snapshot = snapshot_from_bars(bars_from_parquet, "VIX75", "M15")
        expected_keys = {
            "symbol",
            "timeframe",
            "ts",
            "close",
            "atr",
            "atr_norm",
            "rsi",
            "ema50",
            "ema200",
            "bb_upper",
            "bb_mid",
            "bb_lower",
            "stoch_k",
            "stoch_d",
            "log_return",
            "realized_vol",
            "swing_high",
            "swing_low",
            "zones",
        }
        assert set(snapshot.keys()) == expected_keys
        assert snapshot["symbol"] == "VIX75"
        assert snapshot["timeframe"] == "M15"
        assert snapshot["rsi"] == pytest.approx(float(snapshot["rsi"]), abs=100.0)
        assert isinstance(snapshot["zones"], list)
        assert snapshot["atr_norm"] == pytest.approx(
            float(snapshot["atr"]) / float(snapshot["close"]), rel=1e-9
        )

    def test_rejects_short_window(self) -> None:
        short = _synthetic_bars(120)
        with pytest.raises(ValueError, match="at least"):
            snapshot_from_bars(short, "VIX75", "M15")
