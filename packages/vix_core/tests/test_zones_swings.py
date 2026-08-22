"""Zone state machine + swing detection tests."""

from datetime import UTC, datetime

import numpy as np
import pytest
from vix_core.schemas import Bar, Zone, ZoneKind, ZoneState
from vix_core.swings import detect_swings
from vix_core.zones import ZoneEngine


def _bar(ts: datetime, high: float, low: float, close: float) -> Bar:
    return Bar(ts=ts, open=(high + low) / 2, high=high, low=low, close=close)


class TestZoneStateMachine:
    def make_zone(self) -> Zone:
        return Zone(
            kind=ZoneKind.DEMAND,
            top=100.0,
            bottom=98.0,
            created_ts=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_fresh_to_tested_on_shallow_touch(self) -> None:
        engine = ZoneEngine()
        zone = self.make_zone()
        # Close stays near the top edge: touch without deep penetration.
        bar = _bar(datetime.now(UTC), high=100.5, low=98.2, close=99.5)
        updated = engine.update(zone, bar, atr_value=1.0)
        assert updated.state is ZoneState.TESTED
        assert updated.touches == 1

    def test_close_below_breaks_demand(self) -> None:
        engine = ZoneEngine()
        zone = self.make_zone()
        # close 2.0 below bottom with ATR 1.0 buffer 0.25 => broken
        bar = _bar(datetime.now(UTC), high=99.0, low=94.0, close=95.5)
        updated = engine.update(zone, bar, atr_value=1.0)
        assert updated.state is ZoneState.BROKEN

    def test_deep_penetration_mitigates(self) -> None:
        engine = ZoneEngine()
        zone = self.make_zone()  # height 2.0; mitigation at >= 1.5 depth
        # Close stays inside the zone but penetrates 1.7 from the top.
        bar = _bar(datetime.now(UTC), high=100.5, low=97.9, close=98.3)
        updated = engine.update(zone, bar, atr_value=1.0)
        assert updated.state is ZoneState.MITIGATED

    def test_broken_is_terminal(self) -> None:
        engine = ZoneEngine()
        zone = self.make_zone().model_copy(update={"state": ZoneState.BROKEN})
        bar = _bar(datetime.now(UTC), high=99.0, low=98.5, close=99.0)
        assert engine.update(zone, bar, atr_value=1.0).state is ZoneState.BROKEN

    def test_no_touch_leaves_untouched(self) -> None:
        engine = ZoneEngine()
        zone = self.make_zone()
        bar = _bar(datetime.now(UTC), high=101.0, low=100.2, close=100.8)
        updated = engine.update(zone, bar, atr_value=1.0)
        assert updated.state is ZoneState.FRESH
        assert updated.touches == 0


class TestBuildZones:
    def test_detects_zone_after_impulse(self) -> None:
        rng = np.random.default_rng(3)
        n = 120
        base = 100.0 + rng.normal(scale=0.05, size=n).cumsum()
        open_ = base.copy()
        close = base + rng.normal(scale=0.02, size=n)
        high = np.maximum(open_, close) + 0.02
        low = np.minimum(open_, close) - 0.02

        ts = np.datetime64("2026-01-01T00:00:00") + np.arange(n).astype("timedelta64[m]")

        # Inject a strong bullish impulse leaving a demand zone behind.
        close[80] = low[79] + 3.0
        high[80] = close[80] + 0.05
        open_[80] = low[79] + 0.05

        engine = ZoneEngine()
        zones = engine.build_zones(open_, high, low, close, ts)
        assert len(zones) >= 1
        kinds = {z.kind for z in zones}
        assert ZoneKind.DEMAND in kinds or ZoneKind.SUPPLY in kinds

    def test_insufficient_history_returns_empty(self) -> None:
        engine = ZoneEngine()
        small = np.ones(10)
        ts = np.arange(
            np.datetime64("2026-01-01T00:00:00"),
            np.datetime64("2026-01-01T00:00:10"),
            dtype="datetime64[s]",
        )
        assert engine.build_zones(small, small, small, small, ts) == ()


class TestSwings:
    def test_detects_obvious_pivots(self) -> None:
        n = 60
        base = np.sin(np.linspace(0, 6 * np.pi, n))
        high = base + 1.0
        low = base - 1.0
        swings = detect_swings(high, low, lookback=5)
        highs = [s for s in swings if s.kind == "high"]
        lows = [s for s in swings if s.kind == "low"]
        assert len(highs) >= 2
        assert len(lows) >= 2
        # Peaks of sin are maxima of high
        peak_price = max(highs, key=lambda s: s.price).price
        assert peak_price == pytest.approx(float(high.max()), abs=1e-9)

    def test_short_series_empty(self) -> None:
        assert detect_swings(np.ones(4), np.ones(4), lookback=5) == ()
