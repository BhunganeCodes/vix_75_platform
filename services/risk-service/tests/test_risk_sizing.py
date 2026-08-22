"""Risk gate tests: clamp-down sizing, stops level, exposure limits."""

import pytest
from app.exposure import ExposureTracker, OpenPosition, now_iso
from app.sizing import size_position
from app.validator import AccountSnapshot, RiskValidator
from vix_core.risk import SizingStatus, SymbolConstraints
from vix_core.schemas import ConfluenceComponents, Direction, RejectionReason, Signal

VIX75_SPEC = SymbolConstraints(
    volume_min=0.01,
    volume_max=100.0,
    volume_step=0.01,
    tick_size=0.01,
    tick_value=1.0,
    point=0.01,
    stops_level_points=50,  # 0.50 in price terms
    margin_per_lot=100.0,
)


def _signal(entry: float = 100.0, sl: float = 95.0) -> Signal:
    from datetime import UTC, datetime

    return Signal(
        created_ts=datetime.now(tz=UTC),
        symbol="Volatility 75 Index",
        ltf_timeframe="M15",
        direction=Direction.BUY,
        entry=entry,
        sl=sl,
        tp1=entry + 2 * (entry - sl),
        tp2=entry + 3 * (entry - sl),
        score=6,
        max_score=7,
        components=ConfluenceComponents(
            zone_touch=True,
            htf_trend_aligned=True,
            bbma_confirm=True,
            regime_ok=True,
            meta_label_ok=True,
        ),
    )


class TestSizing:
    def test_small_account_wide_sl_never_clamps_up(self) -> None:
        """The legacy EA bug: lots below volume_min must be REJECTED."""
        result = size_position(
            _signal(),
            account=AccountSnapshot(balance=20.0, equity=20.0, margin_free=20.0),
            constraints=VIX75_SPEC,
            risk_pct=1.0,  # $0.20 risk vs $500/lot loss -> 0.0004 lots
            margin_usage_cap=0.5,
        )
        assert result.status is SizingStatus.REJECT_RISK_TOO_SMALL
        assert result.lots == 0.0  # never bumped to volume_min
        assert "clamp up" in result.reason.lower()

    def test_healthy_sizing_passes(self) -> None:
        result = size_position(
            _signal(),
            account=AccountSnapshot(balance=10_000.0, equity=10_000.0, margin_free=9_000.0),
            constraints=VIX75_SPEC,
            risk_pct=1.0,  # $100 risk; loss/lot = (5/0.01)*1 = 500 -> 0.20 lots
            margin_usage_cap=0.5,
        )
        assert result.status is SizingStatus.OK
        assert result.lots == pytest.approx(0.20)

    def test_margin_budget_reduces_lots_down(self) -> None:
        # Risk wants 0.20 lots (needs 20 margin); cap is 50% of 30 free =
        # 15 budget -> size DOWN to 0.15 lots instead of rejecting.
        result = size_position(
            _signal(),
            account=AccountSnapshot(balance=10_000.0, equity=10_000.0, margin_free=30.0),
            constraints=VIX75_SPEC,
            risk_pct=1.0,
            margin_usage_cap=0.5,
        )
        assert result.status is SizingStatus.OK
        assert result.lots == pytest.approx(0.15)
        assert result.lots * VIX75_SPEC.margin_per_lot <= 30.0 * 0.5

    def test_margin_below_volume_min_rejected(self) -> None:
        # Even volume_min (0.01 x 100 = 1.0 margin) exceeds the usable
        # budget -> clean REJECT_MARGIN, never a forced trade.
        result = size_position(
            _signal(),
            account=AccountSnapshot(balance=10_000.0, equity=10_000.0, margin_free=1.0),
            constraints=VIX75_SPEC,
            risk_pct=1.0,
            margin_usage_cap=0.5,  # budget 0.50 < 1.00 minimum requirement
        )
        assert result.status is SizingStatus.REJECT_MARGIN
        assert result.lots == 0.0


class TestStopsValidation:
    @pytest.mark.asyncio
    async def test_sl_inside_stops_level_rejected(self) -> None:
        validator = RiskValidator(redis_client=None)
        tight = _signal(entry=100.0, sl=99.8)  # 0.20 < 0.50 minimum distance
        detail = validator.check_stops(tight, VIX75_SPEC)
        assert detail is not None and "SL" in detail

    @pytest.mark.asyncio
    async def test_valid_distances_pass(self) -> None:
        validator = RiskValidator(redis_client=None)
        assert validator.check_stops(_signal(), VIX75_SPEC) is None


class TestExposureLimits:
    def _position(self, key: str, risk: float) -> OpenPosition:
        return OpenPosition(
            idempotency_key=key,
            signal_id=f"sig-{key}",
            symbol="Volatility 75 Index",
            direction=Direction.BUY,
            lots=0.2,
            entry=100.0,
            sl=95.0,
            risk_amount=risk,
            opened_ts=now_iso(),
        )

    @pytest.mark.asyncio
    async def test_max_open_trades_reached(self) -> None:
        tracker = ExposureTracker(None)
        for i in range(3):
            await tracker.register(self._position(f"k{i}", risk=50.0))

        ok, reason = await tracker.preflight(balance=10_000.0)
        assert ok is False
        assert reason is RejectionReason.MAX_OPEN_TRADES_REACHED

    @pytest.mark.asyncio
    async def test_total_risk_cap_reached(self) -> None:
        tracker = ExposureTracker(None, max_open_trades=10, max_total_risk_pct=3.0)
        for i in range(3):
            await tracker.register(self._position(f"r{i}", risk=100.0))  # $300 = 3%

        ok, reason = await tracker.preflight(balance=10_000.0)
        assert ok is False
        assert reason is RejectionReason.MAX_TOTAL_RISK_EXCEEDED

    @pytest.mark.asyncio
    async def test_release_frees_capacity(self) -> None:
        tracker = ExposureTracker(None, max_open_trades=1)
        await tracker.register(self._position("only", risk=10.0))
        ok, _ = await tracker.preflight(balance=10_000.0)
        assert ok is False

        await tracker.release("only")
        ok, reason = await tracker.preflight(balance=10_000.0)
        assert ok is True and reason is None
