"""Vectorized swing/pivot detection via ``scipy.signal.find_peaks``.

A swing high is a local maximum with ``lookback`` bars of lower highs on
each side; a swing low is the mirror. ``find_peaks`` gives us this in
O(n) with plateau handling, replacing the O(n^2) rescan pattern found
in the legacy codebase (vix75_phase2_structure_engine.py:174).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from scipy.signal import find_peaks

FloatArray = npt.NDArray[np.float64]

__all__ = ["Swing", "detect_swings"]


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    price: float
    kind: str  # "high" | "low"


def detect_swings(
    high: FloatArray,
    low: FloatArray,
    *,
    lookback: int = 5,
) -> tuple[Swing, ...]:
    """Return confirmed swing highs/lows.

    Args:
        high: High prices.
        low: Low prices.
        lookback: Bars required on each side to confirm a pivot.

    Returns:
        Chronologically ordered swings (highs and lows interleaved).
    """
    if high.shape != low.shape:
        raise ValueError("high/low shape mismatch")
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    if high.size < 2 * lookback + 1:
        return ()

    distance = 2 * lookback + 1
    high_idx, _ = find_peaks(high, distance=distance)
    low_idx, _ = find_peaks(-low, distance=distance)

    swings = [
        *[Swing(index=int(i), price=float(high[i]), kind="high") for i in high_idx],
        *[Swing(index=int(i), price=float(low[i]), kind="low") for i in low_idx],
    ]
    swings.sort(key=lambda s: s.index)
    return tuple(swings)


def last_swing_before(swings: tuple[Swing, ...], index: int, kind: str) -> Swing | None:
    """Most recent swing of `kind` strictly before bar `index`."""
    candidate: Swing | None = None
    for swing in swings:
        if swing.index >= index:
            break
        if swing.kind == kind:
            candidate = swing
    return candidate
