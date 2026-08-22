"""Multi-timeframe confluence engine (pure, no I/O).

The engine receives everything it needs as typed inputs - the consumer is
responsible for fetching HTF features from TimescaleDB and regime/meta
snapshots from Redis. This keeps every gate deterministic and unit-testable
without containers or infrastructure mocks.

Hard gates (per spec):

1. Zone touch: LTF close must sit inside an active HTF/MTF S/D zone.
2. HTF alignment: direction must agree with the EMA50/200 macro trend
   unless S0-fading is explicitly enabled.
3. Regime: HMM state must not be S0 unless fading; unknown regimes fail
   closed.
4. Meta-label: LightGBM P(win) must clear ``min_p_win``.

SL/TP construction: SL sits beyond the zone edge by ``sl_atr_buffer`` x
ATR; TP1/TP2 are R-multiples of that risk distance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vix_core.schemas import (
    ConfluenceComponents,
    Direction,
    RegimeState,
    RejectionReason,
    Signal,
    SignalStatus,
    Zone,
    ZoneKind,
)
from vix_core.scoring import ConfluenceInputs, calculate_score

__all__ = [
    "EvaluationResult",
    "LtfSnapshot",
    "MarketContext",
    "SignalEngine",
    "htf_trend_from_emas",
]


@dataclass(frozen=True, slots=True)
class LtfSnapshot:
    """Latest LTF feature row enriched with its active zones."""

    symbol: str
    timeframe: str
    ts: datetime
    close: float
    atr: float
    rsi: float
    ema50: float
    ema200: float
    zones: tuple[Zone, ...] = ()


@dataclass(frozen=True, slots=True)
class MarketContext:
    htf_trend: str  # "up" | "down" | "flat"
    regime: str  # RegimeState value or "unknown"
    p_win: float | None = None
    rsi_divergence: bool | None = None  # dedicated detector lands later


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    signal: Signal | None
    score: float
    max_score: int
    rejections: tuple[str, ...]


def htf_trend_from_emas(ema50: float, ema200: float) -> str:
    """Macro trend label from the HTF moving-average relationship."""
    if ema50 > ema200:
        return "up"
    if ema50 < ema200:
        return "down"
    return "flat"


class SignalEngine:
    """Stateless confluence evaluator; construct with thresholds, reuse."""

    __slots__ = (
        "allow_s0_fade",
        "min_confluence",
        "min_p_win",
        "rr_tp_1",
        "rr_tp_2",
        "sl_atr_buffer",
    )

    def __init__(
        self,
        *,
        min_confluence: float = 4.5,
        min_p_win: float = 0.55,
        allow_s0_fade: bool = False,
        sl_atr_buffer: float = 0.5,
        rr_tp_1: float = 2.0,
        rr_tp_2: float = 3.0,
    ) -> None:
        self.min_confluence = min_confluence
        self.min_p_win = min_p_win
        self.allow_s0_fade = allow_s0_fade
        self.sl_atr_buffer = sl_atr_buffer
        self.rr_tp_1 = rr_tp_1
        self.rr_tp_2 = rr_tp_2

    def evaluate(self, ltf: LtfSnapshot, market: MarketContext) -> EvaluationResult:
        touched = self._zone_touch(ltf)
        if touched is None:
            return self._reject("no_active_zone_touch")

        zone_kind, direction = touched
        regime_state = self._resolve_regime(market.regime)
        if regime_state is None:
            return self._reject(
                f"{RejectionReason.INVALID_SIGNAL.value}: unknown HMM regime "
                f"{market.regime!r} (fail-closed)"
            )
        if regime_state is RegimeState.S0_RANGE and not self.allow_s0_fade:
            return self._reject(f"regime_gate_blocked: {regime_state.value}")

        # HTF alignment is a hard entry requirement (spec): counter-trend
        # entries are allowed ONLY when the S0 fade mode is enabled.
        trend_aligned = (direction is Direction.BUY and market.htf_trend == "up") or (
            direction is Direction.SELL and market.htf_trend == "down"
        )
        if not trend_aligned and not self.allow_s0_fade:
            return self._reject(
                f"htf_alignment_blocked: {direction.value} against "
                f"{market.htf_trend} macro trend"
            )

        inputs = ConfluenceInputs(
            direction=direction,
            htf_trend=market.htf_trend,
            regime=regime_state,
            zone_kind_touched=zone_kind,
            bbma_confirms=self._bbma_proxy(ltf),
            rsi_divergence=bool(market.rsi_divergence),
            p_win=market.p_win,
            allow_s0_fade=self.allow_s0_fade,
        )
        result = calculate_score(inputs, min_score=self.min_confluence, min_p_win=self.min_p_win)
        if not result.passed:
            return EvaluationResult(
                signal=None,
                score=float(result.score),
                max_score=result.max_score,
                rejections=result.rejections,
            )

        signal = self._build_signal(ltf, direction, zone_kind, result.score, result.components)
        return EvaluationResult(
            signal=signal,
            score=float(result.score),
            max_score=result.max_score,
            rejections=(),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _reject(reason: str) -> EvaluationResult:
        return EvaluationResult(signal=None, score=0.0, max_score=7, rejections=(reason,))

    def _zone_touch(self, ltf: LtfSnapshot) -> tuple[ZoneKind, Direction] | None:
        """Active zone containing the close, mapped to a trade direction."""
        for zone in ltf.zones:
            if zone.state.value in {"broken", "mitigated"}:
                continue
            if not zone.bottom <= ltf.close <= zone.top:
                continue
            if zone.kind is ZoneKind.DEMAND:
                return ZoneKind.DEMAND, Direction.BUY
            if zone.kind is ZoneKind.SUPPLY:
                return ZoneKind.SUPPLY, Direction.SELL
        return None

    def _bbma_proxy(self, ltf: LtfSnapshot) -> bool:
        """Simplified BBMA confirmation until the dedicated module lands.

        Momentum with room: long side valid while RSI < 70 and price holds
        the fast reference (ema50); short side mirrored below ema50 with
        RSI > 30. Both sides may be true in wide equilibria - scoring then
        simply loses no weight.
        """
        above = ltf.close >= ltf.ema50 and ltf.rsi < 70
        below = ltf.close <= ltf.ema50 and ltf.rsi > 30
        return above or below

    @staticmethod
    def _resolve_regime(raw: str) -> RegimeState | None:
        try:
            return RegimeState(raw)
        except ValueError:
            return None

    def _zone_edge(self, zone_kind: ZoneKind, ltf: LtfSnapshot) -> float:
        for zone in ltf.zones:
            if zone.kind is zone_kind and zone.bottom <= ltf.close <= zone.top:
                return zone.bottom if zone_kind is ZoneKind.DEMAND else zone.top
        return ltf.close

    def _build_signal(
        self,
        ltf: LtfSnapshot,
        direction: Direction,
        zone_kind: ZoneKind,
        score: int,
        components: ConfluenceComponents,
    ) -> Signal:
        entry = ltf.close
        edge = self._zone_edge(zone_kind, ltf)
        if direction is Direction.BUY:
            sl = min(edge - self.sl_atr_buffer * ltf.atr, entry - 0.1 * ltf.atr)
            risk = entry - sl
            tp1 = entry + self.rr_tp_1 * risk
            tp2 = entry + self.rr_tp_2 * risk
        else:
            sl = max(edge + self.sl_atr_buffer * ltf.atr, entry + 0.1 * ltf.atr)
            risk = sl - entry
            tp1 = entry - self.rr_tp_1 * risk
            tp2 = entry - self.rr_tp_2 * risk

        return Signal(
            created_ts=ltf.ts,
            symbol=ltf.symbol,
            ltf_timeframe=ltf.timeframe,
            direction=direction,
            entry=round(entry, 5),
            sl=round(sl, 5),
            tp1=round(tp1, 5),
            tp2=round(tp2, 5),
            score=int(score),
            max_score=7,
            components=components,
            p_win=None,
            status=SignalStatus.PROPOSED,
        )
