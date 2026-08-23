"""Pure chart rendering tests with synthetic data (no I/O)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.charts import (
    render_ml_explainability,
    render_regime_chart,
    render_strategy_chart,
)


def _ohlcv(n: int = 20) -> dict:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    return {
        "ts": [start + timedelta(minutes=15 * i) for i in range(n)],
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.5 + 0.1 * i for i in range(n)],
        "volume": [1000] * n,
    }


def _zones() -> list[dict]:
    return [
        {"kind": "demand", "top": 101.5, "bottom": 100.0},
        {"kind": "supply", "top": 105.0, "bottom": 103.5},
    ]


def _signals(n: int = 3) -> list[dict]:
    return [
        {
            "created_ts": datetime(2026, 8, 2, tzinfo=UTC) + timedelta(hours=i),
            "direction": "BUY" if i % 2 == 0 else "SELL",
            "entry": 100.5 + i * 0.5,
            "sl": 99.0,
            "tp1": 103.0,
            "score": 6,
            "max_score": 7,
        }
        for i in range(n)
    ]


class TestStrategyChart:
    def test_returns_dict_with_data(self) -> None:
        result = render_strategy_chart(_ohlcv(), _zones(), _signals())
        assert isinstance(result, dict)
        assert "data" in result
        assert "layout" in result

    def test_contains_candlestick_trace(self) -> None:
        result = render_strategy_chart(_ohlcv(30), _zones(), _signals())
        candle_traces = [t for t in result["data"] if t.get("type") == "candlestick"]
        assert len(candle_traces) >= 1

    def test_zone_shapes_present(self) -> None:
        result = render_strategy_chart(_ohlcv(), _zones(), [])
        shapes = result.get("layout", {}).get("shapes", [])
        assert len(shapes) >= 2  # one per zone

    def test_signal_markers_present(self) -> None:
        result = render_strategy_chart(_ohlcv(), [], _signals(4))
        scatter_traces = [t for t in result["data"] if t.get("type") == "scatter"]
        assert len(scatter_traces) == 4

    def test_layout_has_dimensions(self) -> None:
        result = render_strategy_chart(_ohlcv(), [], [])
        assert result.get("layout", {}).get("height") == 600

    def test_empty_ohlcv_does_not_crash(self) -> None:
        result = render_strategy_chart({"ts": []}, [], [])
        assert isinstance(result, dict)


class TestRegimeChart:
    def test_basic_rendering(self) -> None:
        n = 30
        features = {
            "ts": [datetime.now(tz=UTC) + timedelta(minutes=15 * i) for i in range(n)],
            "close": [100.0 + 0.1 * i for i in range(n)],
            "regime_id": [0] * 15 + [1] * 15,
            "zones": [[] for _ in range(n)],
        }
        result = render_regime_chart(_ohlcv(n), features)
        assert isinstance(result, dict)
        assert "data" in result
        line_traces = [t for t in result["data"] if t.get("type") == "scatter"]
        assert len(line_traces) >= 1

    def test_empty_features_no_crash(self) -> None:
        result = render_regime_chart(_ohlcv(), {"ts": [], "regime_id": [], "zones": []})
        assert isinstance(result, dict)


class TestMLExplainability:
    def test_feature_importance_bar(self) -> None:
        importance = [("rsi", 120), ("ema50", 80), ("atr_norm", 60)]
        result = render_ml_explainability(importance, {"ts": [], "p_up": []})
        bar_traces = [t for t in result["data"] if t.get("type") == "bar"]
        assert len(bar_traces) == 1

    def test_proba_timeline_line(self) -> None:
        timeline = {
            "ts": [datetime.now(tz=UTC) + timedelta(hours=i) for i in range(10)],
            "p_up": [0.55 + 0.03 * i for i in range(10)],
        }
        result = render_ml_explainability([], timeline)
        scatter_traces = [t for t in result["data"] if t.get("type") == "scatter"]
        assert len(scatter_traces) == 1

    def test_no_data_renders_annotation(self) -> None:
        result = render_ml_explainability([], {"ts": [], "p_up": []})
        annotations = result.get("layout", {}).get("annotations", [])
        assert any("No model" in str(a.get("text", "")) for a in annotations)
