"""Regression tests for the audit fixes baked into vix_core.risk."""

import pytest
from vix_core.risk import (
    LotSizingResult,
    SizingStatus,
    SymbolConstraints,
    compute_lots,
    validate_stop_distances,
)


@pytest.fixture()
def vix75_constraints() -> SymbolConstraints:
    """Representative Deriv VIX75-ish broker constraints."""
    return SymbolConstraints(
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        tick_size=0.01,
        tick_value=0.10,  # per 1.0 lot per tick (illustrative)
        point=0.01,
        stops_level_points=50,
        margin_per_lot=25.0,
    )


class TestComputeLots:
    def test_happy_path_sizes_to_risk(self, vix75_constraints: SymbolConstraints) -> None:
        # risk 1% of 10_000 = 100; SL distance = 5.00 => 500 ticks
        # loss_per_lot = 500 * 0.10 = 50 => raw lots = 2.0
        result = compute_lots(
            equity=10_000.0,
            risk_pct=1.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            constraints=vix75_constraints,
            free_margin=10_000.0,
        )
        assert isinstance(result, LotSizingResult)
        assert result.status is SizingStatus.OK
        assert result.lots == pytest.approx(2.0)
        assert result.risk_amount == pytest.approx(100.0)

    def test_small_account_never_clamps_up(self, vix75_constraints: SymbolConstraints) -> None:
        # Risk budget buys less than volume_min worth of exposure.
        result = compute_lots(
            equity=10.0,
            risk_pct=1.0,  # 0.10 risk; loss_per_lot 50 => raw 0.002
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            constraints=vix75_constraints,
        )
        assert result.lots == 0.0
        assert result.status is SizingStatus.REJECT_RISK_TOO_SMALL
        assert "clamp up" in result.reason

    def test_floor_rounds_down_not_up(self, vix75_constraints: SymbolConstraints) -> None:
        # raw lots = 100/50 * ... engineered to land between steps
        result = compute_lots(
            equity=12_345.0,
            risk_pct=1.0,  # 123.45 risk / 50 per lot = 2.469 -> 2.46
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            constraints=vix75_constraints,
        )
        assert result.lots == pytest.approx(2.46)

    def test_rejects_when_sl_inside_stops_level(self, vix75_constraints: SymbolConstraints) -> None:
        result = compute_lots(
            equity=10_000.0,
            risk_pct=1.0,
            entry=100.0,
            stop_loss=99.99,  # 0.01 < stops level 0.50
            take_profit=101.0,
            constraints=vix75_constraints,
        )
        assert result.status is SizingStatus.REJECT_STOPS_LEVEL

    def test_margin_shortfall_rejects(self, vix75_constraints: SymbolConstraints) -> None:
        # Even volume_min (0.01 lots x 25.0 margin = 0.25) must exceed the
        # usable budget for a clean REJECT_MARGIN.
        result = compute_lots(
            equity=10_000.0,
            risk_pct=1.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            constraints=vix75_constraints,
            free_margin=0.10,  # budget 0.095 < 0.25 minimum requirement
        )
        assert result.status is SizingStatus.REJECT_MARGIN
        assert result.lots == 0.0

    def test_margin_reduces_lots_down_never_up(self, vix75_constraints: SymbolConstraints) -> None:
        # Risk wants 2.0 lots; margin only affords 1.5 -> size down to fit.
        result = compute_lots(
            equity=10_000.0,
            risk_pct=1.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            constraints=vix75_constraints,
            free_margin=37.5,  # budget 35.625 -> max affordable 1.42 lots
        )
        assert result.status is SizingStatus.OK
        assert result.lots == pytest.approx(1.42)
        budget = 37.5 * 0.95
        assert result.lots * vix75_constraints.margin_per_lot <= budget

    def test_invalid_inputs_rejected(self, vix75_constraints: SymbolConstraints) -> None:
        result = compute_lots(
            equity=-1.0,
            risk_pct=1.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            constraints=vix75_constraints,
        )
        assert result.status is SizingStatus.REJECT_INVALID_INPUT


class TestValidateStopDistances:
    def test_ok_distances_pass(self, vix75_constraints: SymbolConstraints) -> None:
        reason = validate_stop_distances(
            entry=100.0,
            stop_loss=98.0,
            take_profit=104.0,
            constraints=vix75_constraints,
        )
        assert reason is None

    def test_tp_too_close_fails(self, vix75_constraints: SymbolConstraints) -> None:
        reason = validate_stop_distances(
            entry=100.0,
            stop_loss=98.0,
            take_profit=100.2,  # 0.20 < 0.50 min
            constraints=vix75_constraints,
        )
        assert reason is not None and "TP" in reason
