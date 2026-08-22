"""Supply/Demand zone detection + lifecycle state machine.

Detection (vectorized):
    A *base* is up to ``max_base_candles`` low-range candles; the candle
    that leaves the base with body >= ``impulse_atr_mult`` x ATR marks
    the origin. The base's high/low becomes the zone.

Lifecycle (online, per closed bar)::

    FRESH --(first touch)--> TESTED --(deep penetration)--> MITIGATED
      |                            |
      +---(close beyond edge)------+----> BROKEN (terminal)

Broken zones are retained for audit but excluded from scoring.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt

from vix_core.indicators import atr as atr_series
from vix_core.logging import get_logger
from vix_core.schemas import Bar, Zone, ZoneKind, ZoneState

FloatArray = npt.NDArray[np.float64]
TimestampArray = npt.NDArray[np.datetime64]

logger = get_logger(__name__)

__all__ = ["ZoneEngine", "ZoneEngineConfig"]


def _to_utc_datetime(value: np.datetime64) -> datetime:
    """Convert a numpy datetime64 (UTC epoch) to tz-aware datetime."""
    seconds = value.astype("datetime64[s]").astype(np.int64)
    return datetime.fromtimestamp(int(seconds), tz=UTC)


class ZoneEngineConfig:
    """Tunables; kept as plain attributes for cheap construction."""

    __slots__ = (
        "atr_period",
        "break_buffer_atr",
        "expiry_bars",
        "impulse_body_atr_mult",
        "max_base_candles",
        "max_zones",
        "mitigation_level",
    )

    def __init__(
        self,
        *,
        impulse_body_atr_mult: float = 1.2,
        max_base_candles: int = 4,
        mitigation_level: float = 0.75,
        break_buffer_atr: float = 0.25,
        expiry_bars: int = 500,
        max_zones: int = 12,
        atr_period: int = 14,
    ) -> None:
        self.impulse_body_atr_mult = impulse_body_atr_mult
        self.max_base_candles = max_base_candles
        self.mitigation_level = mitigation_level
        self.break_buffer_atr = break_buffer_atr
        self.expiry_bars = expiry_bars
        self.max_zones = max_zones
        self.atr_period = atr_period


class ZoneEngine:
    """Builds S/D zones from OHLC arrays and advances their states."""

    def __init__(self, config: ZoneEngineConfig | None = None) -> None:
        self._cfg = config or ZoneEngineConfig()
        self.zones: list[Zone] = []

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def build_zones(
        self,
        open_: FloatArray,
        high: FloatArray,
        low: FloatArray,
        close: FloatArray,
        timestamps: TimestampArray,
    ) -> tuple[Zone, ...]:
        """Detect zones over a history window; replaces internal list."""
        if not (open_.shape == high.shape == low.shape == close.shape):
            raise ValueError("OHLC arrays must share one shape")
        if open_.size != timestamps.size:
            raise ValueError("timestamps length must match OHLC")
        if open_.size < self._cfg.atr_period + 5:
            return ()

        body = np.abs(close - open_)
        avg_body = np.convolve(body, np.ones(20) / 20.0, mode="same")
        atr_values = atr_series(high, low, close, period=self._cfg.atr_period)
        threshold = self._cfg.impulse_body_atr_mult * np.nan_to_num(atr_values)

        impulse_mask = body > np.maximum(threshold, avg_body * 1.5)

        found: list[Zone] = []
        n = open_.size
        i = self._cfg.atr_period + 1
        while i < n - 1:
            if not impulse_mask[i]:
                i += 1
                continue

            # Walk back over the base preceding the impulse candle.
            base_start = i - 1
            base_len = 0
            while (
                base_start >= 0
                and base_len < self._cfg.max_base_candles
                and body[base_start] <= np.maximum(avg_body[base_start], 1e-12)
            ):
                base_start -= 1
                base_len += 1
            base_start += 1  # last valid base bar

            if base_len >= 1:
                kind = ZoneKind.SUPPLY if close[i] < open_[i] else ZoneKind.DEMAND
                top = float(np.max(high[base_start : i + 1]))
                bottom = float(np.min(low[base_start : i + 1]))
                found.append(
                    Zone(
                        kind=kind,
                        top=top,
                        bottom=bottom,
                        created_ts=_to_utc_datetime(timestamps[i]),
                        score=int(base_len),
                    )
                )
            i += base_len + 1

        found = found[-self._cfg.max_zones :]
        self.zones = list(found)
        logger.info("zones built", count=len(self.zones))
        return tuple(found)

    # ------------------------------------------------------------------
    # Online state machine
    # ------------------------------------------------------------------

    def update(self, zone: Zone, bar: Bar, atr_value: float) -> Zone:
        """Advance one zone through one closed bar; returns updated zone."""
        if zone.state is ZoneState.BROKEN:
            return zone

        buffer = self._cfg.break_buffer_atr * atr_value
        touched = bar.high >= zone.bottom and bar.low <= zone.top
        zone_height = zone.top - zone.bottom
        # Penetration depth measured from the edge price ENTERS through:
        # demand is entered from above (depth from top), supply from below.
        if zone.kind is ZoneKind.DEMAND:
            clamped = min(max(bar.close, zone.bottom), zone.top)
            depth = zone.top - clamped
            broken = bar.close < zone.bottom - buffer
        else:
            clamped = min(max(bar.close, zone.bottom), zone.top)
            depth = clamped - zone.bottom
            broken = bar.close > zone.top + buffer
        mitigated = zone_height > 0 and depth >= self._cfg.mitigation_level * zone_height

        if broken:
            return zone.model_copy(update={"state": ZoneState.BROKEN})

        updates: dict[str, object] = {}
        if touched:
            updates["touches"] = zone.touches + 1
            if zone.state is ZoneState.FRESH:
                updates["state"] = ZoneState.TESTED
        if touched and mitigated:
            updates["state"] = ZoneState.MITIGATED

        if updates:
            return zone.model_copy(update=updates)
        return zone

    def update_all(self, bar: Bar, atr_value: float) -> tuple[Zone, ...]:
        self.zones = [self.update(z, bar, atr_value) for z in self.zones]
        active = [
            z
            for z in self.zones
            if z.state is not ZoneState.BROKEN and z.touches <= self._cfg.expiry_bars
        ]
        return tuple(active)
