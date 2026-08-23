"""Pure Plotly chart builders - no I/O, easily unit-testable."""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

DARK_TEMPLATE = "plotly_dark"

COLOR_BULL = "#26a69a"
COLOR_BEAR = "#ef5350"
COLOR_SUPPLY = "rgba(239,83,80,0.15)"
COLOR_DEMAND = "rgba(38,166,154,0.15)"
REGIME_COLORS = {
    0: "rgba(158,158,158,0.12)",  # S0 range - grey
    1: "rgba(76,175,80,0.10)",  # S1 up - green tint
    2: "rgba(244,67,54,0.10)",  # S2 down - red tint
}
REGIME_LABELS = {0: "S0 Range", 1: "S1 Up", 2: "S2 Down"}


def render_strategy_chart(
    ohlcv: dict[str, list[Any]],
    zones: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> dict[str, Any]:
    fig = go.Figure()

    if ohlcv.get("ts"):
        fig.add_trace(
            go.Candlestick(
                x=ohlcv["ts"],
                open=ohlcv["open"],
                high=ohlcv["high"],
                low=ohlcv["low"],
                close=ohlcv["close"],
                name="VIX75",
                increasing_line_color=COLOR_BULL,
                decreasing_line_color=COLOR_BEAR,
            )
        )

    for zone in zones:
        kind = zone.get("kind", "demand")
        top, bottom = float(zone.get("top", 0)), float(zone.get("bottom", 0))
        color = COLOR_DEMAND if kind == "demand" else COLOR_SUPPLY
        fig.add_hrect(
            y0=bottom,
            y1=top,
            fillcolor=color,
            opacity=0.4,
            annotation_text=kind.upper(),
            annotation_font_size=9,
            layer="below",
        )

    for sig in signals:
        direction = str(sig.get("direction", ""))
        ts = sig.get("created_ts")
        entry = sig.get("entry")
        color = COLOR_BULL if direction == "BUY" else COLOR_BEAR
        marker_symbol = "triangle-up" if direction == "BUY" else "triangle-down"
        fig.add_trace(
            go.Scatter(
                x=[ts],
                y=[entry],
                mode="markers",
                marker=dict(symbol=marker_symbol, size=12, color=color),
                name=f"{direction} {sig.get('score','')}/{sig.get('max_score','')}",
                hovertext=(f"E={entry} SL={sig.get('sl')} TP1={sig.get('tp1')}"),
            )
        )

    fig.update_layout(
        template=DARK_TEMPLATE,
        title="VIX75 Strategy Chart",
        xaxis_rangeslider_visible=False,
        height=600,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return cast_dict(fig.to_plotly_json())


def render_regime_chart(
    ohlcv: dict[str, list[Any]],
    features: dict[str, list[Any]],
) -> dict[str, Any]:
    fig = go.Figure()

    regime_ids = [int(r) if r is not None else -1 for r in features.get("regime_id", [])]
    f_ts = features.get("ts", [])

    # Draw background bands for contiguous regime spans.
    if regime_ids:
        span_start = 0
        for i in range(1, len(regime_ids) + 1):
            if i == len(regime_ids) or regime_ids[i] != regime_ids[span_start]:
                rid = regime_ids[span_start]
                t0 = f_ts[span_start] if f_ts else None
                t1 = f_ts[min(i, len(f_ts) - 1)] if f_ts else None
                if t0 and t1 and rid in REGIME_COLORS:
                    fig.add_vrect(
                        x0=t0,
                        x1=t1,
                        fillcolor=REGIME_COLORS[rid],
                        opacity=0.6,
                        layer="below",
                        annotation_text=REGIME_LABELS.get(rid, ""),
                        annotation_font_size=10,
                    )
                span_start = i

    if ohlcv.get("ts"):
        fig.add_trace(
            go.Scatter(
                x=ohlcv["ts"],
                y=ohlcv["close"],
                mode="lines",
                name="Close",
                line=dict(color="#e0e0e0", width=1.2),
            )
        )

    fig.update_layout(
        template=DARK_TEMPLATE,
        title="HMM Regime Classification",
        height=400,
        showlegend=True,
    )
    return cast_dict(fig.to_plotly_json())


def render_ml_explainability(
    feature_importance: list[tuple[str, float]],
    proba_timeline: dict[str, list[Any]],
) -> dict[str, Any]:
    fig = go.Figure()

    if feature_importance:
        names, values = zip(*feature_importance)
        fig.add_trace(
            go.Bar(
                x=list(names),
                y=list(values),
                orientation="h",
                marker_color=COLOR_BULL,
                name="Importance",
            )
        )
    else:
        fig.add_annotation(text="No model artifact available", showarrow=False)

    if proba_timeline.get("ts"):
        fig.add_trace(
            go.Scatter(
                x=proba_timeline["ts"],
                y=proba_timeline["p_up"],
                mode="lines+markers",
                name="P(win)",
                line=dict(color=COLOR_BULL, width=1.5),
                yaxis="y2",
            )
        )

    fig.update_layout(
        template=DARK_TEMPLATE,
        title="ML Explainability",
        yaxis=dict(title="Feature Importance"),
        yaxis2=dict(title="P(win)", overlaying="y", side="right", range=[0, 1]),
        height=500,
    )
    return cast_dict(fig.to_plotly_json())


def cast_dict(fig_json: Any) -> dict[str, Any]:
    """Plotly's to_plotly_json returns Any; narrow for mypy."""
    import typing

    return typing.cast(dict[str, Any], fig_json)
