"""Confluence engine tests: bullish generation + regime gate rejection."""

from datetime import UTC, datetime

import pytest
from app.engine import (
    EvaluationResult,
    LtfSnapshot,
    MarketContext,
    SignalEngine,
    htf_trend_from_emas,
)
from vix_core.schemas import Direction, Zone, ZoneKind, ZoneState


def _demand_zone(top: float = 100.5, bottom: float = 99.0) -> Zone:
    return Zone(
        kind=ZoneKind.DEMAND,
        top=top,
        bottom=bottom,
        created_ts=datetime(2026, 1, 1, tzinfo=UTC),
        state=ZoneState.FRESH,
    )


def _bullish_snapshot(**overrides: object) -> LtfSnapshot:
    values: dict[str, object] = {
        "symbol": "Volatility 75 Index",
        "timeframe": "M15",
        "ts": datetime(2026, 2, 2, 10, 45, tzinfo=UTC),
        "close": 100.0,
        "atr": 1.0,
        "rsi": 55.0,
        "ema50": 99.5,
        "ema200": 98.0,
        "zones": (_demand_zone(),),
    }
    values.update(overrides)
    return LtfSnapshot(**values)  # type: ignore[arg-type]


BULLISH_MARKET = MarketContext(htf_trend="up", regime="S1_trend_up", p_win=0.80)


class TestSignalGeneration:
    def test_bullish_setup_generates_buy(self) -> None:
        engine = SignalEngine()
        result = engine.evaluate(_bullish_snapshot(), BULLISH_MARKET)

        assert isinstance(result, EvaluationResult)
        assert result.signal is not None
        assert result.signal.direction is Direction.BUY
        assert result.signal.status.value == "proposed"
        assert result.score >= 4.5
        assert result.rejections == ()

    def test_sl_tp_geometry_from_zone_edge_and_atr(self) -> None:
        engine = SignalEngine(sl_atr_buffer=0.5, rr_tp_1=2.0, rr_tp_2=3.0)
        snap = _bullish_snapshot()
        result = engine.evaluate(snap, BULLISH_MARKET)

        sig = result.signal
        assert sig is not None
        # SL sits half an ATR below the zone bottom (99.0 - 0.5).
        assert sig.sl == pytest.approx(98.5)
        # Risk = entry - sl; TPs are exact R multiples.
        risk = sig.entry - sig.sl
        assert sig.tp1 == pytest.approx(sig.entry + 2.0 * risk)
        assert sig.tp2 == pytest.approx(sig.entry + 3.0 * risk)
        assert sig.sl < sig.entry < sig.tp1 < sig.tp2

    def test_sell_setup_at_supply_zone(self) -> None:
        supply = Zone(
            kind=ZoneKind.SUPPLY,
            top=101.0,
            bottom=99.5,
            created_ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        snap = _bullish_snapshot(close=100.5, rsi=35.0, ema50=101.5, zones=(supply,))
        market = MarketContext(htf_trend="down", regime="S2_trend_down", p_win=0.75)
        result = SignalEngine().evaluate(snap, market)

        assert result.signal is not None
        assert result.signal.direction is Direction.SELL
        assert result.signal.sl > result.signal.entry > result.signal.tp1


class TestHardGates:
    def test_s0_regime_rejects(self) -> None:
        market = MarketContext(htf_trend="up", regime="S0_range", p_win=0.80)
        result = SignalEngine().evaluate(_bullish_snapshot(), market)

        assert result.signal is None
        joined = " ".join(result.rejections)
        assert "regime_gate_blocked" in joined
        assert "S0_range" in joined

    def test_s0_fade_mode_allows_range_trade(self) -> None:
        market = MarketContext(htf_trend="flat", regime="S0_range", p_win=0.70)
        result = SignalEngine(allow_s0_fade=True).evaluate(_bullish_snapshot(), market)
        assert result.signal is not None

    def test_low_meta_label_blocks_hard(self) -> None:
        market = MarketContext(htf_trend="up", regime="S1_trend_up", p_win=0.40)
        result = SignalEngine().evaluate(_bullish_snapshot(), market)
        assert result.signal is None
        assert any("P(win)" in r for r in result.rejections)

    def test_unknown_regime_fails_closed(self) -> None:
        market = MarketContext(htf_trend="up", regime="garbage", p_win=0.9)
        result = SignalEngine().evaluate(_bullish_snapshot(), market)
        assert result.signal is None
        assert any("unknown HMM regime" in r for r in result.rejections)

    def test_no_zone_touch_no_signal(self) -> None:
        snap = _bullish_snapshot(zones=())
        result = SignalEngine().evaluate(snap, BULLISH_MARKET)
        assert result.signal is None
        assert result.rejections == ("no_active_zone_touch",)

    def test_counter_trend_blocked_without_fade(self) -> None:
        result = SignalEngine().evaluate(
            _bullish_snapshot(),
            MarketContext(htf_trend="down", regime="S1_trend_up", p_win=0.8),
        )
        # Score loses HTF weight AND regime weight vs S2; hard gates on
        # meta still pass but total drops below confluence_min.
        assert result.signal is None


class TestHelpers:
    def test_htf_trend_labels(self) -> None:
        assert htf_trend_from_emas(105.0, 95.0) == "up"
        assert htf_trend_from_emas(94.0, 95.0) == "down"
        assert htf_trend_from_emas(100.0, 100.0) == "flat"

    def test_broken_zones_ignored(self) -> None:
        broken = _demand_zone().model_copy(update={"state": ZoneState.BROKEN})
        result = SignalEngine().evaluate(_bullish_snapshot(zones=(broken,)), BULLISH_MARKET)
        assert result.rejections == ("no_active_zone_touch",)
