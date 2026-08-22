"""Domain schemas shared across services (Pydantic v2).

These models are the *contract* between data, feature, ml, signal,
risk and execution services. Keep them transport-serializable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model: immutable-ish, forbids unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class Bar(StrictModel):
    ts: datetime  # UTC, tz-aware
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0


class BarSeries(StrictModel):
    symbol: str
    timeframe: str  # "M5" | "M15" | "H1" | "H4" | "D1"
    bars: tuple[Bar, ...] = ()

    def __len__(self) -> int:
        return len(self.bars)


# ---------------------------------------------------------------------------
# Zones / structure
# ---------------------------------------------------------------------------


class ZoneKind(StrEnum):
    SUPPLY = "supply"
    DEMAND = "demand"


class ZoneState(StrEnum):
    FRESH = "fresh"
    TESTED = "tested"
    MITIGATED = "mitigated"
    BROKEN = "broken"


class Zone(StrictModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: ZoneKind
    top: float
    bottom: float
    created_ts: datetime
    state: ZoneState = ZoneState.FRESH
    touches: int = 0
    score: int = 0


# ---------------------------------------------------------------------------
# Regime (HMM) and meta-labels (LightGBM)
# ---------------------------------------------------------------------------


class RegimeState(StrEnum):
    S0_RANGE = "S0_range"
    S1_TREND_UP = "S1_trend_up"
    S2_TREND_DOWN = "S2_trend_down"


class RegimeSnapshot(StrictModel):
    ts: datetime
    timeframe: str
    regime_id: int  # raw HMM state index
    regime: RegimeState
    probabilities: tuple[float, float, float]


# ---------------------------------------------------------------------------
# Signals & orders
# ---------------------------------------------------------------------------


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    SHADOW_FILLED = "shadow_filled"


class ConfluenceComponents(StrictModel):
    htf_trend_aligned: bool = False
    zone_touch: bool = False
    bbma_confirm: bool = False
    rsi_divergence: bool = False
    regime_ok: bool = False
    meta_label_ok: bool = False


class Signal(StrictModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_ts: datetime
    symbol: str
    ltf_timeframe: str
    direction: Direction
    entry: float
    sl: float
    tp1: float
    tp2: float
    score: int
    max_score: int
    components: ConfluenceComponents
    p_win: float | None = None  # LightGBM meta-label probability
    status: SignalStatus = SignalStatus.PROPOSED


class OrderRequest(StrictModel):
    """Idempotent order intent - execution service dedupes on key."""

    idempotency_key: str
    signal_id: str
    symbol: str
    direction: Direction
    lots: float
    entry: float
    sl: float
    tp: float
    deviation_points: int = 30


class OrderResult(StrictModel):
    idempotency_key: str
    accepted: bool
    retcode: int | None = None
    retcode_description: str | None = None
    ticket: int | None = None
    price: float | None = None
    comment: str | None = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(StrictModel):
    service: str
    status: str = "ok"
    version: str = "0.1.0"
