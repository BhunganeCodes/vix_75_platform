"""Multi-timeframe confluence scoring engine.

Combines HTF trend, HMM regime, LTF triggers and the LightGBM
meta-label into a single gated decision. All inputs are plain typed
values so the whole engine is deterministic and unit-testable.

Gating rules (from the strategy spec):

1. LTF price must touch an active HTF/MTF supply/demand zone.
2. Direction must align with the HTF trend unless S0-fading is enabled.
3. BBMA and RSI-divergence must both confirm the direction.
4. HMM regime must not be S0 (range) unless explicitly fading it.
5. LightGBM meta-label P(win) must exceed ``min_p_win`` (0.55 default).
"""

from __future__ import annotations

from dataclasses import dataclass

from vix_core.schemas import ConfluenceComponents, Direction, RegimeState, ZoneKind

__all__ = ["ConfluenceInputs", "ConfluenceResult", "ConfluenceScorer"]


@dataclass(frozen=True, slots=True)
class ConfluenceInputs:
    direction: Direction
    htf_trend: str  # "up" | "down" | "flat" (EMA 50/200 on H4/D1)
    regime: RegimeState
    zone_kind_touched: ZoneKind | None  # active S/D zone under/at price
    bbma_confirms: bool
    rsi_divergence: bool
    p_win: float | None = None  # meta-label probability

    # Feature toggles (strategy config)
    allow_s0_fade: bool = False


@dataclass(frozen=True, slots=True)
class ConfluenceResult:
    passed: bool
    score: int
    max_score: int
    components: ConfluenceComponents
    rejections: tuple[str, ...]


class ConfluenceScorer:
    """Stateless scorer; construct with thresholds and reuse."""

    __slots__ = ("min_p_win", "min_score")

    def __init__(self, *, min_score: int = 5, min_p_win: float = 0.55) -> None:
        self.min_score = min_score
        self.min_p_win = min_p_win

    def evaluate(self, inputs: ConfluenceInputs) -> ConfluenceResult:
        c = ConfluenceComponents()
        rejections: list[str] = []
        score = 0

        # --- Component 1: zone touch (weight 2) ------------------------
        zone_matches = inputs.zone_kind_touched is not None and (
            (inputs.direction is Direction.BUY and inputs.zone_kind_touched is ZoneKind.DEMAND)
            or (inputs.direction is Direction.SELL and inputs.zone_kind_touched is ZoneKind.SUPPLY)
        )
        if zone_matches:
            c = c.model_copy(update={"zone_touch": True})
            score += 2
        else:
            rejections.append("no matching S/D zone touch")

        # --- Component 2: HTF trend alignment (weight 1) ---------------
        trend_ok = (inputs.direction is Direction.BUY and inputs.htf_trend == "up") or (
            inputs.direction is Direction.SELL and inputs.htf_trend == "down"
        )
        fading_range = inputs.allow_s0_fade and inputs.regime is RegimeState.S0_RANGE
        if trend_ok or fading_range:
            c = c.model_copy(update={"htf_trend_aligned": True})
            score += 1
        else:
            rejections.append("direction counter to HTF trend without fade mode")

        # --- Components 3/4: BBMA + RSI divergence (weights 1+1) -------
        if inputs.bbma_confirms:
            c = c.model_copy(update={"bbma_confirm": True})
            score += 1
        else:
            rejections.append("BBMA confirmation missing")
        if inputs.rsi_divergence:
            c = c.model_copy(update={"rsi_divergence": True})
            score += 1
        else:
            rejections.append("RSI divergence missing")

        # --- Component 5: HMM regime gate (weight 1) --------------------
        regime_ok = (
            (inputs.direction is Direction.BUY and inputs.regime is RegimeState.S1_TREND_UP)
            or (inputs.direction is Direction.SELL and inputs.regime is RegimeState.S2_TREND_DOWN)
            or (inputs.allow_s0_fade and inputs.regime is RegimeState.S0_RANGE)
        )
        if regime_ok:
            c = c.model_copy(update={"regime_ok": True})
            score += 1
        else:
            rejections.append(f"HMM regime {inputs.regime} blocks {inputs.direction}")

        # --- Component 6: meta-label gate (weight 1) ---------------------
        if inputs.p_win is not None and inputs.p_win > self.min_p_win:
            c = c.model_copy(update={"meta_label_ok": True})
            score += 1
        else:
            rejections.append(
                "meta-label missing/below threshold"
                if inputs.p_win is None
                else f"P(win)={inputs.p_win:.3f} <= {self.min_p_win}"
            )

        # --- Hard gates ---------------------------------------------------
        # Zone touch, regime and meta-label are HARD gates per spec:
        # a high score cannot override them.
        hard_fail = (
            not zone_matches
            or not c.regime_ok
            or inputs.p_win is None
            or inputs.p_win <= self.min_p_win
        )
        passed = (not hard_fail) and score >= self.min_score
        return ConfluenceResult(
            passed=passed,
            score=score,
            max_score=7,
            components=c,
            rejections=tuple(rejections),
        )
