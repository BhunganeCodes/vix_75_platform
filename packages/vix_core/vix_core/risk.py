"""Position sizing and pre-trade validation.

Fixes the audit findings on the legacy EA (`vix75ea.mq5:335-353`):

* Calculated lots below ``volume_min`` are REJECTED, never clamped up -
  a small account must not silently exceed its configured risk.
* Broker constraints are validated explicitly:
  ``SYMBOL_TRADE_STOPS_LEVEL`` distance and free-margin coverage.
* Everything is a pure function of injected inputs, so sizing math is
  unit-testable without an MT5 terminal attached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "LotSizingResult",
    "SizingStatus",
    "SymbolConstraints",
    "compute_lots",
    "validate_stop_distances",
]


class SizingStatus(StrEnum):
    OK = "ok"
    REJECT_RISK_TOO_SMALL = "reject_risk_too_small"  # would need clamp-up
    REJECT_INVALID_INPUT = "reject_invalid_input"
    REJECT_MARGIN = "reject_margin"
    REJECT_STOPS_LEVEL = "reject_stops_level"
    CAPPED_BY_VOLUME_MAX = "capped_by_volume_max"


@dataclass(frozen=True, slots=True)
class SymbolConstraints:
    """Snapshot of broker/symbol trading conditions."""

    volume_min: float
    volume_max: float
    volume_step: float
    tick_size: float  # price change of one tick
    tick_value: float  # account-currency value of one tick per 1.0 lot
    point: float  # symbol point size (e.g. 0.01)
    stops_level_points: int  # SYMBOL_TRADE_STOPS_LEVEL
    margin_per_lot: float  # initial margin for 1.0 lot (account ccy)


@dataclass(frozen=True, slots=True)
class LotSizingResult:
    status: SizingStatus
    lots: float
    reason: str
    risk_amount: float


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0.0:
        raise ValueError("volume_step must be positive")
    steps = math.floor(round(value / step, 10))
    return round(steps * step, 10)


def validate_stop_distances(
    *,
    entry: float,
    stop_loss: float,
    take_profit: float,
    constraints: SymbolConstraints,
) -> str | None:
    """Return a rejection reason or None when distances are tradable."""
    min_dist = constraints.stops_level_points * constraints.point
    sl_dist = abs(entry - stop_loss)
    tp_dist = abs(take_profit - entry)
    if min_dist <= 0.0:
        return None
    if sl_dist < min_dist:
        return f"SL distance {sl_dist} < stops level {min_dist}"
    if tp_dist < min_dist:
        return f"TP distance {tp_dist} < stops level {min_dist}"
    return None


def compute_lots(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    take_profit: float,
    constraints: SymbolConstraints,
    free_margin: float | None = None,
    margin_buffer: float = 0.95,
) -> LotSizingResult:
    """Risk-based lot sizing with strict clamp-DOWN semantics.

    Args:
        equity: Account equity in account currency.
        risk_pct: Percent of equity to risk (e.g. ``1.0``).
        entry: Intended entry price.
        stop_loss: Stop-loss price.
        take_profit: Take-profit price (validated against stops level).
        constraints: Broker symbol constraints snapshot.
        free_margin: Current free margin, when known from the terminal.
        margin_buffer: Fraction of free margin usable (safety haircut).

    Returns:
        ``LotSizingResult``; ``lots`` is only non-zero when
        ``status is SizingStatus.OK`` (or CAPPED_BY_VOLUME_MAX).
    """
    risk_amount = equity * risk_pct / 100.0

    if equity <= 0.0 or risk_pct <= 0.0:
        return LotSizingResult(
            SizingStatus.REJECT_INVALID_INPUT,
            0.0,
            "equity/risk_pct must be positive",
            0.0,
        )
    if constraints.tick_size <= 0.0 or constraints.tick_value <= 0.0:
        return LotSizingResult(
            SizingStatus.REJECT_INVALID_INPUT, 0.0, "invalid tick_size/tick_value", 0.0
        )
    if entry <= 0.0 or stop_loss <= 0.0:
        return LotSizingResult(
            SizingStatus.REJECT_INVALID_INPUT, 0.0, "prices must be positive", 0.0
        )

    stops_reason = validate_stop_distances(
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        constraints=constraints,
    )
    if stops_reason is not None:
        return LotSizingResult(SizingStatus.REJECT_STOPS_LEVEL, 0.0, stops_reason, risk_amount)

    sl_distance = abs(entry - stop_loss)
    loss_per_lot = (sl_distance / constraints.tick_size) * constraints.tick_value
    if loss_per_lot <= 0.0:
        return LotSizingResult(
            SizingStatus.REJECT_INVALID_INPUT,
            0.0,
            "loss_per_lot computed as zero",
            risk_amount,
        )

    raw_lots = risk_amount / loss_per_lot
    lots = _floor_to_step(raw_lots, constraints.volume_step)

    # ---- THE RULE: clamp DOWN, never up -----------------------------
    if lots < constraints.volume_min:
        # Rounding down dropped us under broker minimum: reject rather
        # than exceed configured risk by bumping up to volume_min.
        return LotSizingResult(
            SizingStatus.REJECT_RISK_TOO_SMALL,
            0.0,
            f"risk-sized {lots} < volume_min {constraints.volume_min}; refusing to clamp up",
            risk_amount,
        )

    if lots > constraints.volume_max:
        lots = constraints.volume_max
        result_status = SizingStatus.CAPPED_BY_VOLUME_MAX
    else:
        result_status = SizingStatus.OK

    # ---- Margin check -------------------------------------------------
    if free_margin is not None:
        required = lots * constraints.margin_per_lot
        budget = free_margin * margin_buffer
        while required > budget and lots > constraints.volume_min:
            lots = max(
                _floor_to_step(lots - constraints.volume_step, constraints.volume_step),
                0.0,
            )
            required = lots * constraints.margin_per_lot
        if required > budget:
            return LotSizingResult(
                SizingStatus.REJECT_MARGIN,
                0.0,
                f"margin {required:.2f} exceeds usable free margin {budget:.2f}",
                risk_amount,
            )
        if lots < constraints.volume_min:
            return LotSizingResult(
                SizingStatus.REJECT_RISK_TOO_SMALL,
                0.0,
                "margin-adjusted lots fell below broker minimum",
                risk_amount,
            )

    return LotSizingResult(result_status, lots, "sized", risk_amount)
